"""SMPP client implementation using smppai."""

import logging
from typing import Dict, Any
from porth.protocols.base import ProtocolHandler
from porth.core.message import SMSMessage
from porth.config.settings import SMPPClientConfig
from porth.core.delivery import DeliveryEngine

logger = logging.getLogger(__name__)


class SMPPClient(ProtocolHandler):
    """SMPP client for sending messages to SMSC."""

    def __init__(self, config: SMPPClientConfig, delivery_engine: DeliveryEngine):
        self.config = config
        self.delivery_engine = delivery_engine
        self.client = None  # smppai client instance
        self.connected = False

    async def start(self) -> None:
        """Start the SMPP client (alias for connect)."""
        await self.connect()

    async def stop(self) -> None:
        """Stop the SMPP client (alias for disconnect)."""
        await self.disconnect()

    async def connect(self) -> None:
        """Connect to SMSC."""
        try:
            # TODO: Initialize smppai client
            # from smppai import SMPPClient as SmppaiClient
            # self.client = SmppaiClient(
            #     host=self.config.host,
            #     port=self.config.port,
            #     system_id=self.config.system_id,
            #     password=self.config.password,
            #     system_type=self.config.system_type
            # )
            # await self.client.connect()

            self.connected = True
            logger.info(
                f'SMPP client connected to {self.config.host}:{self.config.port}'
            )

        except Exception as e:
            logger.error(f'Failed to connect SMPP client: {e}')
            raise

    async def disconnect(self) -> None:
        """Disconnect from SMSC."""
        try:
            if self.client and self.connected:
                # TODO: Disconnect smppai client
                # await self.client.disconnect()
                pass

            self.connected = False
            logger.info('SMPP client disconnected')

        except Exception as e:
            logger.error(f'Error disconnecting SMPP client: {e}')

    async def send_message(self, message: SMSMessage) -> Dict[str, Any]:
        """Send SMS message via SMPP."""
        if not self.connected:
            raise Exception('SMPP client not connected')

        try:
            # TODO: Send message using smppai
            # result = await self.client.submit_sm(
            #     source_addr=message.source_addr,
            #     destination_addr=message.destination_addr,
            #     short_message=message.message_text.encode('utf-8')
            # )

            # Simulate successful send
            result = {
                'message_id': message.message_id,
                'status': 'submitted',
                'smsc_message_id': f'smsc_{message.message_id}',
            }

            logger.info(f'Message {message.message_id} sent via SMPP')
            return result

        except Exception as e:
            logger.error(f'Failed to send message via SMPP: {e}')
            raise

    async def handle_delivery_receipt(self, receipt_data: Dict[str, Any]) -> None:
        """Handle delivery receipt from SMSC."""
        try:
            # Process DLR and notify delivery engine
            await self.delivery_engine.send_delivery_receipt(receipt_data)
            logger.info(f'Processed SMPP delivery receipt: {receipt_data}')

        except Exception as e:
            logger.error(f'Error handling SMPP delivery receipt: {e}')
