"""Main application entry point."""

import asyncio
import logging
import signal
from typing import List

from aiohttp import web

from porth.config.settings import Settings
from porth.core.queue import MessageQueue
from porth.core.delivery import DeliveryEngine
from porth.protocols.http.api import create_http_app
from porth.protocols.smpp.server import SMPPServer
from porth.protocols.smpp.client import SMPPClient


class SMSGateway:
    """Main SMS Gateway application."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.message_queue = MessageQueue()
        self.delivery_engine = DeliveryEngine(self.message_queue, settings)
        self.servers: List[web.BaseRunner] = []
        self.smpp_servers: List[SMPPServer] = []
        self.smpp_clients: List[SMPPClient] = []

    async def start(self):
        """Start all gateway components."""
        # Start delivery engine
        await self.delivery_engine.start()

        # Start HTTP API server
        http_app = create_http_app(self.message_queue, self.settings)
        http_runner = web.AppRunner(http_app)
        await http_runner.setup()

        http_site = web.TCPSite(
            http_runner, self.settings.http.host, self.settings.http.port
        )
        await http_site.start()
        self.servers.append(http_runner)

        # Start SMPP servers (if configured)
        for smpp_config in self.settings.smpp.servers:
            smpp_server = SMPPServer(smpp_config, self.message_queue)
            await smpp_server.start()
            self.smpp_servers.append(smpp_server)

        # Start SMPP clients (if configured)
        for client_config in self.settings.smpp.clients:
            smpp_client = SMPPClient(client_config, self.delivery_engine)
            await smpp_client.connect()
            self.smpp_clients.append(smpp_client)

        logging.info('SMS Gateway started successfully')

    async def stop(self):
        """Stop all gateway components."""
        logging.info('Stopping SMS Gateway...')

        # Stop SMPP clients
        for client in self.smpp_clients:
            try:
                await client.disconnect()
            except Exception as e:
                logging.error(f'Error stopping SMPP client: {e}')

        # Stop SMPP servers
        for server in self.smpp_servers:
            try:
                await server.stop()
            except Exception as e:
                logging.error(f'Error stopping SMPP server: {e}')

        # Stop HTTP servers
        for runner in self.servers:
            try:
                if runner._server is not None:  # Check if server exists before cleanup
                    await runner.cleanup()
            except Exception as e:
                logging.error(f'Error stopping HTTP server: {e}')

        # Stop delivery engine
        try:
            await self.delivery_engine.stop()
        except Exception as e:
            logging.error(f'Error stopping delivery engine: {e}')

        logging.info('SMS Gateway stopped')


async def main():
    """Main entry point."""
    # Setup logging
    logging.basicConfig(level=logging.INFO)

    # Load initial settings to check for config file
    settings = Settings()

    # If a config file is specified, load settings from it
    if settings.config_file:
        settings = Settings.load_from_file(settings.config_file)

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
    asyncio.run(main())
