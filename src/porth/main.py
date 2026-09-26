"""Main application entry point."""

import asyncio
import logging
import signal

from aiohttp import web

from porth.config.settings import HTTPConfig, KannelConfig, Settings, load_settings
from porth.core.delivery import DeliveryEngine
from porth.core.queue import MessageQueue
from porth.core.store import MessageStore
from porth.protocols.http.api import create_http_app
from porth.protocols.kannel.api import create_kannel_app
from porth.protocols.smpp.client import SMPPClient


class SMSGateway:
    """Main SMS Gateway application."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.message_queue = MessageQueue()
        self.message_store = MessageStore()
        self.delivery_engine = DeliveryEngine(
            self.message_queue, self.message_store, settings
        )
        self.servers: list[web.BaseRunner] = []
        self.smpp_clients: list[SMPPClient] = []

    async def start(self):
        """Start all gateway components."""
        # Start delivery engine
        await self.delivery_engine.start()

        # Start the HTTP API and the Kannel-compatible API, each on its own listener
        await self._serve(
            create_http_app(self.message_queue, self.message_store, self.settings),
            self.settings.http,
        )
        await self._serve(
            create_kannel_app(self.message_queue, self.message_store),
            self.settings.kannel,
        )

        # Start SMPP clients (if configured)
        for client_config in self.settings.smpp.clients:
            smpp_client = SMPPClient(client_config)
            self.delivery_engine.smpp_client = smpp_client
            try:
                await smpp_client.connect()
            except Exception as e:
                # The first send retries the bind (lazy reconnect).
                logging.error(f'SMPP client bind failed at startup: {e}')
            self.smpp_clients.append(smpp_client)

        logging.info('SMS Gateway started successfully')

    async def _serve(self, app: web.Application, config: HTTPConfig | KannelConfig):
        runner = web.AppRunner(app)
        await runner.setup()
        self.servers.append(runner)  # before the bind, so stop() cleans up a failed one
        await web.TCPSite(runner, config.host, config.port).start()

    async def stop(self):
        """Stop all gateway components."""
        logging.info('Stopping SMS Gateway...')

        # Stop delivery engine first, so no worker lazily rebinds a stopped client
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

        # Stop HTTP servers
        for runner in self.servers:
            try:
                if runner._server is not None:  # Check if server exists before cleanup
                    await runner.cleanup()
            except Exception as e:
                logging.error(f'Error stopping HTTP server: {e}')

        logging.info('SMS Gateway stopped')


async def main():
    """Main entry point."""
    # Setup logging
    logging.basicConfig(level=logging.INFO)

    gateway = SMSGateway(load_settings())
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
    asyncio.run(main())
