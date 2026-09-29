"""Main application entry point."""

import argparse
import asyncio
import logging
import signal
import typing as tp

from aiohttp import web
from monobase.config import setup_logging
from monobase.db import make_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from porth.config.settings import HTTPConfig, KannelConfig, Settings, load_settings
from porth.core.delivery import DeliveryEngine
from porth.core.dlr import DLRHandler
from porth.core.mo import MOHandler
from porth.core.queue import MessageQueue
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
        self.message_queue = MessageQueue()
        self.delivery_engine = DeliveryEngine(self.message_queue, uow_factory, settings)
        self.dlr_handler = DLRHandler(uow_factory)
        self.mo_handler = MOHandler(self.message_queue, uow_factory, settings.mo)
        self.servers: list[web.BaseRunner] = []
        self.smpp_clients: list[SMPPClient] = []

    async def start(self):
        """Start all gateway components."""
        # Before any worker, listener or bind: what a previous run accepted but did
        # not send goes first, oldest first (sent ones are not sent again). An
        # unreachable database fails the start here.
        async with self.uow_factory() as uow:
            unsent = await uow.messages.unsent()
        for message in unsent:
            await self.message_queue.put(message)
        if unsent:
            logging.info(f'Re-queued {len(unsent)} message(s) from the store')

        # Before any bind (a worker's send binds too), so the first receipt or MO can
        # already be handled
        await self.dlr_handler.start()
        await self.dlr_handler.resume()
        await self.mo_handler.start()

        # Start the HTTP API and the Kannel-compatible API, each on its own listener
        await self._serve(
            create_http_app(self.message_queue, self.uow_factory, self.settings),
            self.settings.http,
        )
        await self._serve(
            create_kannel_app(
                self.message_queue,
                self.uow_factory,
                default_sender=self.settings.kannel.default_sender,
            ),
            self.settings.kannel,
        )

        # The SMPP client (if configured) before the workers, so a re-queued message
        # finds its client instead of spending an attempt
        if self.settings.smpp.client is not None:
            smpp_client = SMPPClient(
                self.settings.smpp.client,
                on_receipt=self.dlr_handler.on_receipt,
                on_mo=self.mo_handler.on_mo,
            )
            self.delivery_engine.smpp_client = smpp_client
            self.smpp_clients.append(smpp_client)
        await self.delivery_engine.start()
        for smpp_client in self.smpp_clients:
            # Binds now; if that fails, the client retries in the background
            smpp_client.start()

        logging.info('SMS Gateway started successfully')

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
        try:
            await self.delivery_engine.stop()
        except Exception as e:
            logging.error(f'Error stopping delivery engine: {e}')

        # Stop SMPP clients
        for client in self.smpp_clients:
            try:
                await client.disconnect()
            except Exception as e:
                logging.error(f'Error stopping SMPP client: {e}')

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
    asyncio.run(main(parser.parse_args().config))
