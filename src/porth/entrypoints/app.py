"""The gateway and the app bin/porth runs: build_app(settings)."""

import asyncio
import contextlib
import fcntl
import functools
import logging
import os
import signal
import typing as tp

import sqlalchemy as sa
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from porth import metrics
from porth.adapters.db import ensure_schema, make_engine, sqlite_path
from porth.adapters.queue import MessageQueue
from porth.adapters.smpp import SMPPClient
from porth.config.settings import KannelConfig, Settings
from porth.entrypoints.http_api import create_http_app
from porth.entrypoints.kannel_api import KannelAccessLogger, create_kannel_app
from porth.service_layer import services
from porth.service_layer.delivery import DeliveryEngine
from porth.service_layer.dlr import DLRHandler
from porth.service_layer.mo import MOHandler
from porth.service_layer.routing import Router
from porth.service_layer.sweep import Sweeper
from porth.service_layer.unit_of_work import AbstractUnitOfWork, SqlAlchemyUnitOfWork

logger = logging.getLogger(__name__)

LOCK_KEY = 0x706F727468  # 'porth' in ASCII
LOCK_CHECK_SECONDS = 10
# idle_session_timeout of the lock's session; above LOCK_CHECK_SECONDS
LOCK_IDLE_SECONDS = 30


class Gateway:
    """Everything but the REST listener: recovery, handlers, the Kannel listener,
    one delivery engine and SMPP client (two with transceiver = false) per SMSC.

    One porth per database: start() takes a PostgreSQL advisory lock, or on SQLite
    an exclusive lock on <database file>.lock, so a second gateway on the same
    database refuses to start instead of sending again."""

    def __init__(
        self,
        settings: Settings,
        uow_factory: tp.Optional[tp.Callable[[], AbstractUnitOfWork]] = None,
    ):
        self.settings = settings
        self._engine: tp.Optional[AsyncEngine] = None
        self._lock: tp.Optional[AsyncConnection] = None
        self._lock_file: tp.Optional[tp.IO[str]] = None  # SQLite's
        self._lock_check: tp.Optional[asyncio.Task] = None
        self._lock_lost = False
        self._stopping = False
        if uow_factory is None:
            if not settings.db:
                raise ValueError('db is not set: set db in [porth]')
            if sqlite_path(settings.db) in ('', ':memory:'):
                raise ValueError(
                    'db: an in-memory SQLite database is not supported; use a file'
                )
            engine = self._engine = make_engine(settings.db)
            uow_factory = lambda: SqlAlchemyUnitOfWork(engine)  # noqa: E731
        self.uow_factory = uow_factory
        self.router = Router(settings.smsc, settings.routing)
        self.queues = {
            name: MessageQueue(settings.delivery.queue_limit) for name in settings.smsc
        }
        self.dlr_handler = DLRHandler(
            uow_factory, rest_window=settings.http.callback_retry_minutes * 60
        )
        self.sweeper = Sweeper(uow_factory, self.dlr_handler, settings.store)
        self.mo_handler = MOHandler(self.queues, uow_factory, settings.mo)
        self.servers: list[web.BaseRunner] = []
        self.smpp_clients: list[SMPPClient] = []
        self.binds: dict[str, list[SMPPClient]] = {}  # per SMSC: one, or tx and rx
        # One engine (queue, workers, client) per SMSC, so a slow or down SMSC holds
        # up only its own messages. Clients are built here, so a bad throughput fails
        # before the store is read; started in start() before the workers, so a
        # re-queued message finds its client instead of spending an attempt. The
        # SMSC's name reaches receipts and MO through these callbacks.
        self.engines: dict[str, DeliveryEngine] = {}
        for name, config in settings.smsc.items():
            on_receipt = functools.partial(self.dlr_handler.on_receipt, smsc=name)
            on_mo = functools.partial(self.mo_handler.on_mo, smsc=name)
            if config.transceiver:
                smpp_client = SMPPClient(name, config, on_receipt, on_mo)
                self.binds[name] = [smpp_client]
            else:
                # The engine sends through tx; rx only binds and takes receipts and MO
                smpp_client = SMPPClient(name, config, on_receipt, on_mo, bind='tx')
                receiver = SMPPClient(
                    name, config, on_receipt, on_mo, bind='rx', port=config.receive_port
                )
                self.binds[name] = [smpp_client, receiver]
            self.engines[name] = DeliveryEngine(
                name,
                smpp_client,
                self.queues[name],
                uow_factory,
                settings,
                self.dlr_handler,
            )
            self.smpp_clients.extend(self.binds[name])
            # Read at scrape; a second gateway in one process (tests) rebinds them
            metrics.waiting.labels(name).set_function(self.queues[name].qsize)
            metrics.retrying.labels(name).set_function(self.queues[name].retrying)
            metrics.bound.labels(name).set_function(
                functools.partial(self._bound, name)
            )

    def _bound(self, name: str) -> bool:
        """The SMSC is bound once all its binds are."""
        return all(client.connected for client in self.binds[name])

    def smsc_state(self) -> dict[str, dict[str, tp.Any]]:
        """Per SMSC: bound, waiting and retrying, for /status and /ready."""
        return {
            name: {
                'bound': self._bound(name),
                'waiting': engine.message_queue.qsize(),
                'retrying': engine.message_queue.retrying(),
            }
            for name, engine in self.engines.items()
        }

    async def start(self):
        """Start all gateway components."""
        # First: a second porth on this database exits before it sends anything.
        # An injected uow_factory (tests) has no engine and takes no lock.
        if self._engine is not None and self._engine.dialect.name == 'sqlite':
            # Not a lock on the database file: closing any descriptor of it drops
            # SQLite's own locks in this process. The OS frees it when porth dies.
            database = tp.cast(str, self._engine.url.database)
            path = f'{database}.lock'
            # Owner-only, as is a new database file below: they hold messages
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            self._lock_file = os.fdopen(fd, 'w')
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    f'Another porth already uses this database ({path} is locked): '
                    f'stop it first'
                ) from None
            # SQLite gives its -wal and -shm files the database file's mode. Only a new
            # file is created here: an existing one is never opened, see above.
            with contextlib.suppress(FileExistsError):
                os.close(os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        elif self._engine is not None:
            # Session-level and in AUTOCOMMIT, so it never idles in a transaction;
            # kept before the query, so stop() closes it even if the query fails
            self._lock = await self._engine.connect()
            await self._lock.execution_options(isolation_level='AUTOCOMMIT')
            # A session whose porth vanished unseen (a lost host, a partition) ends
            # LOCK_IDLE_SECONDS after its last check, and frees the lock, instead of
            # after TCP keepalive
            await self._lock.execute(
                sa.text(f"SET idle_session_timeout = '{LOCK_IDLE_SECONDS}s'")
            )
            locked = await self._lock.scalar(
                sa.text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK_KEY}
            )
            if not locked:
                raise RuntimeError(
                    f'Another porth already uses this database (advisory lock '
                    f'{LOCK_KEY} is held): stop it first'
                )
            self._lock_check = asyncio.create_task(self._check_lock())
        # After the lock, so a second porth never races the first one's schema
        if self._engine is not None:
            await ensure_schema(self._engine)

        # Before any worker, listener or bind: what a previous run accepted but did
        # not send goes first. An unreachable database fails the start here.
        await services.recover(
            self.uow_factory, self.router, self.queues, self.dlr_handler
        )

        # Before any bind (a worker's send binds too), so the first receipt or MO can
        # already be handled
        await self.dlr_handler.start()
        await self.dlr_handler.resume()
        # After recovery and resume(), so its first pass sees every recovered message
        self.sweeper.start()
        await self.mo_handler.start()

        # The Kannel-compatible API on its own listener; run_app serves the REST one
        await self._serve(
            create_kannel_app(
                self.queues,
                self.router,
                self.uow_factory,
                default_sender=self.settings.kannel.default_sender,
                users=self.settings.kannel.users,
            ),
            self.settings.kannel,
            access_log_class=KannelAccessLogger,  # no query string: it holds passwords
        )

        for engine in self.engines.values():
            await engine.start()
        for smpp_client in self.smpp_clients:
            # Binds now; if that fails, the client retries in the background
            smpp_client.start()

        logging.info('SMS Gateway started successfully')

    async def _check_lock(self):
        """Shut down once the lock's session is gone (a database restart, a dropped
        connection): another porth may hold the lock by now."""
        # ponytail: polled, so up to LOCK_CHECK_SECONDS of sending after the loss
        # (twice that on a silent drop); LISTEN on the lock connection if that matters
        assert self._lock is not None
        while True:
            await asyncio.sleep(LOCK_CHECK_SECONDS)
            # Bounded: a silently dropped connection never answers, and cancelling its
            # query waits for the server too, so the cancel is not awaited
            probe = asyncio.ensure_future(self._lock.execute(sa.text('SELECT 1')))
            done, _ = await asyncio.wait({probe}, timeout=LOCK_CHECK_SECONDS)
            if not done:
                probe.cancel()
                error = f'no answer in {LOCK_CHECK_SECONDS} s'
            elif probe.exception() is not None:
                error = str(probe.exception())
            else:
                continue
            logging.error(f'Lost the database advisory lock, shutting down: {error}')
            self._lock_lost = True
            if not self._stopping:  # a second SIGTERM would raise inside the cleanup
                os.kill(os.getpid(), signal.SIGTERM)  # run_app shuts down gracefully
            # ponytail: a shutdown stuck on the dead server (a stop() awaiting a cancelled
            # query) is cut off whole; bound each stop step if a clean exit matters there
            asyncio.get_running_loop().call_later(3 * LOCK_CHECK_SECONDS, os._exit, 1)
            return

    async def _serve(
        self,
        app: web.Application,
        config: KannelConfig,
        **runner_kwargs: tp.Any,
    ):
        runner = web.AppRunner(app, **runner_kwargs)
        await runner.setup()
        self.servers.append(runner)  # before the bind, so stop() cleans up a failed one
        await web.TCPSite(runner, config.host, config.port).start()

    async def stop(self):
        """Stop all gateway components."""
        logging.info('Stopping SMS Gateway...')
        # The lock check runs to the end, so its session never idles out (and frees the
        # lock) during a slow stop; a loss found now only logs and arms the forced exit
        self._stopping = True

        # Stop delivery engine first, so no worker lazily rebinds a stopped client;
        # disconnect() stops the client's own rebind loop
        for engine in self.engines.values():
            try:
                await engine.stop()
            except Exception as e:
                logger.error(
                    f'SMSC {engine.smsc}: Error stopping delivery engine: {e}',
                    extra={'smsc': engine.smsc},
                )

        # Stop SMPP clients
        for client in self.smpp_clients:
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(
                    f'SMSC {client.label}: Error stopping SMPP client: {e}',
                    extra={'smsc': client.name},
                )

        # Before the DLR handler, whose session dispatches the sweep's calls
        await self.sweeper.stop()
        # After the clients, so no receipt or MO arrives for a closed session
        try:
            await self.dlr_handler.stop()
        except Exception as e:
            logging.error(f'Error stopping DLR handler: {e}')
        try:
            await self.mo_handler.stop()
        except Exception as e:
            logging.error(f'Error stopping MO handler: {e}')

        # Stop HTTP servers
        for runner in self.servers:
            try:
                if runner._server is not None:  # Check if server exists before cleanup
                    await runner.cleanup()
            except Exception as e:
                logging.error(f'Error stopping HTTP server: {e}')

        # Last: everything above may still write. Closed explicitly: dispose()
        # leaves a checked-out connection, and with it the session and the lock.
        if self._lock_check is not None:
            self._lock_check.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._lock_check
        if self._lock is not None:
            try:
                await self._lock.close()
            except Exception as e:
                logging.error(f'Error releasing the database advisory lock: {e}')
        if self._engine is not None:
            # After a lost lock the server may never answer a graceful close
            await self._engine.dispose(close=not self._lock_lost)
        if self._lock_file is not None:
            self._lock_file.close()  # after the engine: nothing writes any more

        logging.info('SMS Gateway stopped')


def build_app(settings: Settings) -> web.Application:
    """The REST app, with the gateway started and stopped around it (cleanup_ctx):
    the REST listener binds after the gateway starts and closes before it stops."""
    gateway = Gateway(settings)
    app = create_http_app(
        gateway.queues,
        gateway.router,
        gateway.uow_factory,
        settings,
        gateway.smsc_state,
    )

    async def run_gateway(app: web.Application) -> tp.AsyncIterator[None]:
        # finally: aiohttp skips the exit of a context whose start raised, and a
        # failed start must still release what it opened
        try:
            await gateway.start()
            yield
        finally:
            await gateway.stop()

    app.cleanup_ctx.append(run_gateway)
    return app
