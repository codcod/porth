"""The gateway and the app bin/porth runs: build_app(settings)."""

import functools
import logging
import typing as tp

from aiohttp import web
from monobase.db import make_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from porth import metrics
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


class Gateway:
    """Everything but the REST listener: recovery, handlers, the Kannel listener,
    one delivery engine and SMPP client (two with transceiver = false) per SMSC."""

    def __init__(
        self,
        settings: Settings,
        uow_factory: tp.Optional[tp.Callable[[], AbstractUnitOfWork]] = None,
    ):
        self.settings = settings
        self._engine: tp.Optional[AsyncEngine] = None
        if uow_factory is None:
            if not settings.db:
                raise ValueError('db is not set: set db in [porth]')
            engine = self._engine = make_engine(settings.db)
            uow_factory = lambda: SqlAlchemyUnitOfWork(engine)  # noqa: E731
        self.uow_factory = uow_factory
        self.router = Router(settings.smsc, settings.routing)
        self.queues = {name: MessageQueue() for name in settings.smsc}
        self.dlr_handler = DLRHandler(uow_factory)
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
            metrics.retrying.labels(name).set_function(
                functools.partial(len, self.engines[name]._retries)
            )
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
                'retrying': len(engine._retries),
            }
            for name, engine in self.engines.items()
        }

    async def start(self):
        """Start all gateway components."""
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

        # Stop delivery engine first, so no worker lazily rebinds a stopped client;
        # disconnect() stops the client's own rebind loop
        for engine in self.engines.values():
            try:
                await engine.stop()
            except Exception as e:
                logging.error(
                    f'SMSC {engine.smsc}: Error stopping delivery engine: {e}'
                )

        # Stop SMPP clients
        for client in self.smpp_clients:
            try:
                await client.disconnect()
            except Exception as e:
                logging.error(f'SMSC {client.label}: Error stopping SMPP client: {e}')

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

        # Last: everything above may still write
        if self._engine is not None:
            await self._engine.dispose()

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
