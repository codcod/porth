"""SMPP client implementation using smppai."""

import asyncio
import logging
import typing as tp

from smpp import Address, DataCoding, RegisteredDelivery
from smpp import SMPPClient as SmppaiClient
from smpp.exceptions import SMPPException, SMPPPDUException
from smpp.gsm import make_parts

from porth.config.settings import SMPPClientConfig
from porth.core.exceptions import MessageError
from porth.core.message import SMSMessage

logger = logging.getLogger(__name__)

TOO_LONG = 'message exceeds one SMS segment; concatenation not supported yet (POR-006)'


def choose_data_coding(text: str) -> DataCoding:
    """
    GSM 03.38 if smppai's codec can carry text, else UCS2 (as smppai's Client.send
    picks). Raise MessageError unless it fits one segment by smppai's segmentation.
    """
    for data_coding in (DataCoding.DEFAULT, DataCoding.UCS2):
        try:
            parts = make_parts(text, data_coding)
        except SMPPPDUException:
            continue  # not representable in this coding
        except ValueError:  # more than 255 parts
            raise MessageError(TOO_LONG)
        if len(parts) > 1:
            raise MessageError(TOO_LONG)
        return data_coding
    raise MessageError('message text cannot be encoded as GSM 03.38 or UCS2')


class SMPPClient:
    """SMPP client for sending messages to SMSC."""

    def __init__(self, config: SMPPClientConfig):
        self.config = config
        self.client: tp.Optional[SmppaiClient] = None
        self._connect_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """True while the smppai client is bound (smppai clears it on unbind or lost connection)."""
        return self.client is not None and self.client.is_bound

    async def connect(self) -> SmppaiClient:
        """Bind a fresh transceiver unless already bound; return the bound client. Raises on failure."""
        async with self._connect_lock:
            if self.client is not None and self.client.is_bound:
                return self.client
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
            # BaseException incl. CancelledError: never abandon a half-open bind.
            except BaseException as e:
                logger.error(f'Failed to connect SMPP client: {e!r}')
                await self._close(client)
                raise

            self.client = client
            logger.info(
                f'SMPP client connected to {self.config.host}:{self.config.port}'
            )
            return client

    async def disconnect(self) -> None:
        """Disconnect from SMSC."""
        client, self.client = self.client, None
        await self._close(client)
        logger.info('SMPP client disconnected')

    async def send_message(self, message: SMSMessage) -> dict[str, tp.Any]:
        """Send SMS message via SMPP, reconnecting lazily if the bind was lost."""
        data_coding = choose_data_coding(message.message_text)
        source = Address.parse(message.source_addr)
        destination = Address.parse(message.destination_addr)

        client = await self.connect()

        try:
            smsc_message_id = await client.submit_sm(
                source_addr=source.addr,
                source_addr_ton=source.ton,
                source_addr_npi=source.npi,
                destination_addr=destination.addr,
                dest_addr_ton=destination.ton,
                dest_addr_npi=destination.npi,
                short_message=message.message_text,
                data_coding=data_coding,
                registered_delivery=RegisteredDelivery.SUCCESS_FAILURE
                if message.dlr_requested
                else RegisteredDelivery.NO_RECEIPT,
            )
        except Exception as e:
            logger.error(f'Failed to send message via SMPP: {e}')
            # An SMSC response with an error command_status came over a healthy bind;
            # anything else (timeout, I/O, not bound) drops it. Drop only the bind that
            # failed; another worker may already have rebound.
            smsc_rejected = (
                isinstance(e, SMPPException) and e.command_status is not None
            )
            if not smsc_rejected and self.client is client:
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
    async def _close(client: tp.Optional[SmppaiClient]) -> None:
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:
            logger.error(f'Error disconnecting SMPP client: {e}')
