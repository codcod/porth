"""Main application entry point."""

import argparse
import asyncio
import functools
import logging
import signal
import typing as tp
from datetime import datetime, timezone

from aiohttp import web
from monobase.config import setup_logging
from monobase.db import make_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from porth.config.settings import HTTPConfig, KannelConfig, Settings, load_settings
from porth.core.delivery import DeliveryEngine
from porth.core.dlr import DLRHandler
from porth.core.exceptions import NoRoute
from porth.core.message import MessageStatus, SMSMessage
from porth.core.mo import MOHandler
from porth.core.queue import MessageQueue
from porth.core.routing import Router
from porth.core.sweep import Sweeper
from porth.protocols.http.api import create_http_app
from porth.protocols.kannel.api import create_kannel_app
from porth.protocols.smpp.client import SMPPClient
from porth.service_layer.unit_of_work import AbstractUnitOfWork, SqlAlchemyUnitOfWork


class SMSGateway:
    """Main SMS Gateway application."""

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
        # One engine (queue, workers, client) per SMSC, so a slow or down SMSC holds
        # up only its own messages. Clients are built here, so a bad throughput fails
        # before the store is read; started in start() before the workers, so a
        # re-queued message finds its client instead of spending an attempt. The
        # SMSC's name reaches receipts and MO through these callbacks.
        self.engines: dict[str, DeliveryEngine] = {}
        for name, config in settings.smsc.items():
            smpp_client = SMPPClient(
                name,
                config,
                on_receipt=functools.partial(self.dlr_handler.on_receipt, smsc=name),
                on_mo=functools.partial(self.mo_handler.on_mo, smsc=name),
            )
            self.engines[name] = DeliveryEngine(
                name,
                smpp_client,
                self.queues[name],
                uow_factory,
                settings,
                self.dlr_handler,
            )
            self.smpp_clients.append(smpp_client)

    async def start(self):
        """Start all gateway components."""
        # Before any worker, listener or bind: what a previous run accepted but did
        # not send goes first, oldest first (sent ones are not sent again). An
        # unreachable database fails the start here.
        async with self.uow_factory() as uow:
            unsent = await uow.messages.unsent()
        requeued = 0
        for message in unsent:
            if message.smsc not in self.engines:
                # Its SMSC was removed from the config, or it predates routing
                await self._reroute(message)
            if message.status != MessageStatus.FAILED:
                assert message.smsc is not None
                await self.queues[message.smsc].put(message)
                requeued += 1
        if requeued:
            logging.info(f'Re-queued {requeued} message(s) from the store')

        # Before any bind (a worker's send binds too), so the first receipt or MO can
        # already be handled
        await self.dlr_handler.start()
        await self.dlr_handler.resume()
        # After recovery and resume(), so its first pass sees every recovered message
        self.sweeper.start()
        await self.mo_handler.start()

        # Start the HTTP API and the Kannel-compatible API, each on its own listener
        await self._serve(
            create_http_app(self.queues, self.router, self.uow_factory, self.settings),
            self.settings.http,
        )
        await self._serve(
            create_kannel_app(
                self.queues,
                self.router,
                self.uow_factory,
                default_sender=self.settings.kannel.default_sender,
            ),
            self.settings.kannel,
        )

        for engine in self.engines.values():
            await engine.start()
        for smpp_client in self.smpp_clients:
            # Binds now; if that fails, the client retries in the background
            smpp_client.start()

        logging.info('SMS Gateway started successfully')

    async def _reroute(self, message: SMSMessage) -> None:
        """Route a recovered message again, or fail it; either way store it."""
        was = message.smsc
        try:
            message.smsc = self.router.route(message.destination_addr)
            logging.info(
                f'Message {message.message_id}: SMSC {was!r} is not configured, '
                f'rerouted to {message.smsc!r}'
            )
        except NoRoute as e:
            message.status = MessageStatus.FAILED
            logging.warning(
                f'Message {message.message_id}: SMSC {was!r} is not configured '
                f'and {e}: failed'
            )
        # A failed one's call is stored, not dispatched: resume() makes it, once
        # the handler has started
        async with self.uow_factory() as uow:
            if message.status == MessageStatus.FAILED:
                now = datetime.now(timezone.utc)
                await self.dlr_handler.finalize(uow, message, now)
            else:
                await uow.messages.update(message)
            await uow.commit()

    async def _serve(self, app: web.Application, config: HTTPConfig | KannelConfig):
        runner = web.AppRunner(app)
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
                logging.error(f'SMSC {client.name}: Error stopping SMPP client: {e}')

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


async def main(config: str):
    """Main entry point."""
    settings = load_settings(config)
    setup_logging(settings.log_level)

    gateway = SMSGateway(settings)
    shutdown_event = asyncio.Event()

    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logging.info(f'Received signal {signum}, initiating shutdown...')
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, signal_handler)

    try:
        await gateway.start()
        logging.info('Porth SMS Gateway is running. Press Ctrl+C to stop.')
        # Wait for shutdown signal
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logging.info('Received KeyboardInterrupt, shutting down...')
    except Exception as e:
        logging.error(f'Unexpected error: {e}')
    finally:
        await gateway.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='porth')
    parser.add_argument('config', nargs='?', default='config/config.toml')
    parser.add_argument(
        '--check',
        action='store_true',
        help='validate the config as startup does (connects to nothing) and exit',
    )
    args = parser.parse_args()
    if args.check:
        SMSGateway(load_settings(args.config))
    else:
        asyncio.run(main(args.config))
