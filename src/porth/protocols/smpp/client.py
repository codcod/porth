"""SMPP client implementation using smppai."""

import asyncio
import logging
from typing import Dict, Any
from smpp import SMPPClient as SmppaiClient, DataCoding
from porth.protocols.base import ProtocolHandler
from porth.core.exceptions import MessageError
from porth.core.message import SMSMessage
from porth.config.settings import SMPPClientConfig
from porth.core.delivery import DeliveryEngine

logger = logging.getLogger(__name__)


def choose_data_coding(text: str) -> int:
    """Pick the single-segment data_coding for text, or raise MessageError if it won't fit."""
    try:
        data_coding, octets = DataCoding.DEFAULT, len(text.encode('gsm0338'))
        limit = 160
    except UnicodeEncodeError:
        data_coding, octets = DataCoding.UCS2, len(text.encode('utf-16-be'))
        limit = 140
    if octets > limit:
        raise MessageError(
            'message exceeds one SMS segment; concatenation not supported yet (POR-006)'
        )
    return data_coding


class SMPPClient(ProtocolHandler):
    """SMPP client for sending messages to SMSC."""

    def __init__(self, config: SMPPClientConfig, delivery_engine: DeliveryEngine):
        self.config = config
        self.delivery_engine = delivery_engine
        self.client = None  # smppai client instance
        self._connect_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """True while the smppai client is bound (smppai clears it on unbind or lost connection)."""
        return self.client is not None and self.client.is_bound

    async def start(self) -> None:
        """Start the SMPP client (alias for connect)."""
        await self.connect()

    async def stop(self) -> None:
        """Stop the SMPP client (alias for disconnect)."""
        await self.disconnect()

    async def connect(self) -> None:
        """Bind a fresh transceiver unless already bound. Raises on failure."""
        async with self._connect_lock:
            if self.connected:
                return
            # A client left over from an unbind or lost connection is dead; close it.
            await self._close(self.client)
            self.client = None
            client = SmppaiClient(
                host=self.config.host,
                port=self.config.port,
                system_id=self.config.system_id,
                password=self.config.password,
                system_type=self.config.system_type,
            )
            try:
                await client.connect()
                await client.bind_transceiver()
            except Exception as e:
                logger.error(f'Failed to connect SMPP client: {e}')
                await self._close(client)
                raise

            self.client = client
            logger.info(
                f'SMPP client connected to {self.config.host}:{self.config.port}'
            )

    async def disconnect(self) -> None:
        """Disconnect from SMSC."""
        client, self.client = self.client, None
        await self._close(client)
        logger.info('SMPP client disconnected')

    async def send_message(self, message: SMSMessage) -> Dict[str, Any]:
        """Send SMS message via SMPP, reconnecting lazily if the bind was lost."""
        data_coding = choose_data_coding(message.message_text)

        await self.connect()
        client = self.client

        try:
            smsc_message_id = await client.submit_sm(
                source_addr=message.source_addr,
                destination_addr=message.destination_addr,
                short_message=message.message_text,
                data_coding=data_coding,
                registered_delivery=1 if message.dlr_requested else 0,
            )
        except Exception as e:
            logger.error(f'Failed to send message via SMPP: {e}')
            # An SMSC response with an error command_status came over a healthy bind;
            # anything else (timeout, I/O, not bound) drops it. Drop only the bind that
            # failed; another worker may already have rebound.
            if getattr(e, 'command_status', None) is None and self.client is client:
                self.client = None
                await self._close(client)
            raise

        logger.info(f'Message {message.message_id} sent via SMPP')
        return {
            'message_id': message.message_id,
            'status': 'submitted',
            'smsc_message_id': smsc_message_id,
        }

    @staticmethod
    async def _close(client: Any) -> None:
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:
            logger.error(f'Error disconnecting SMPP client: {e}')

    async def handle_delivery_receipt(self, receipt_data: Dict[str, Any]) -> None:
        """Handle delivery receipt from SMSC."""
        try:
            # Process DLR and notify delivery engine
            await self.delivery_engine.send_delivery_receipt(receipt_data)
            logger.info(f'Processed SMPP delivery receipt: {receipt_data}')

        except Exception as e:
            logger.error(f'Error handling SMPP delivery receipt: {e}')
