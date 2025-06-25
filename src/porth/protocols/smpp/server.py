"""SMPP server implementation using smppai."""

import logging
from typing import Dict, Any
from porth.protocols.base import ProtocolHandler
from porth.core.message import SMSMessage
from porth.config.settings import SMPPServerConfig
from porth.core.queue import MessageQueue

logger = logging.getLogger(__name__)


class SMPPServer(ProtocolHandler):
    """SMPP server for receiving messages from ESMEs."""

    def __init__(self, config: SMPPServerConfig, message_queue: MessageQueue):
        self.config = config
        self.message_queue = message_queue
        self.server = None  # smppai server instance
        self.running = False

    async def start(self) -> None:
        """Start the SMPP server."""
        try:
            # TODO: Initialize smppai server
            # from smppai import SMPPServer as SmppaiServer
            # self.server = SmppaiServer(
            #     host=self.config.host,
            #     port=self.config.port,
            #     system_id=self.config.system_id,
            #     password=self.config.password
            # )
            #
            # # Set up message handlers
            # self.server.on_submit_sm = self._handle_submit_sm
            #
            # await self.server.start()

            self.running = True
            logger.info(f'SMPP server started on {self.config.host}:{self.config.port}')

        except Exception as e:
            logger.error(f'Failed to start SMPP server: {e}')
            raise

    async def stop(self) -> None:
        """Stop the SMPP server."""
        try:
            if self.server and self.running:
                # TODO: Stop smppai server
                # await self.server.stop()
                pass

            self.running = False
            logger.info('SMPP server stopped')

        except Exception as e:
            logger.error(f'Error stopping SMPP server: {e}')

    async def send_message(self, message: SMSMessage) -> Dict[str, Any]:
        """Send message (not applicable for server)."""
        raise NotImplementedError('SMPP server cannot send messages')

    async def handle_delivery_receipt(self, receipt_data: Dict[str, Any]) -> None:
        """Handle delivery receipt (not applicable for server)."""
        raise NotImplementedError('SMPP server does not handle delivery receipts')

    async def _handle_submit_sm(self, pdu) -> None:
        """Handle incoming submit_sm PDU from ESME."""
        try:
            # Convert SMPP PDU to internal message format
            message = SMSMessage(
                source_addr=pdu.source_addr,
                destination_addr=pdu.destination_addr,
                message_text=pdu.short_message.decode('utf-8'),
                protocol='smpp',
                protocol_data={
                    'system_id': pdu.system_id,
                    'sequence_number': pdu.sequence_number,
                },
            )

            # Add to message queue for processing
            await self.message_queue.put(message)

            logger.info(
                f'Received SMPP message {message.message_id} from {message.source_addr}'
            )

            # TODO: Send submit_sm_resp back to ESME

        except Exception as e:
            logger.error(f'Error handling SMPP submit_sm: {e}')
