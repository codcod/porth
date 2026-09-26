"""SMPP client implementation using smppai."""

import asyncio
import logging
import typing as tp

from smpp import Address, Client, DataCoding, Message, RegisteredDelivery
from smpp import SMPPClient as SmppaiClient
from smpp.exceptions import SMPPException, SMPPPDUException
from smpp.gsm import make_parts

from porth.config.settings import SMPPClientConfig
from porth.core.exceptions import MessageError
from porth.core.message import SMSMessage

logger = logging.getLogger(__name__)


def choose_data_coding(text: str) -> DataCoding:
    """
    GSM 03.38 if smppai's codec can carry text, else UCS2 (as smppai's Client.send
    picks). Raise MessageError if smppai's segmentation needs more than 255 parts.
    """
    for data_coding in (DataCoding.DEFAULT, DataCoding.UCS2):
        try:
            make_parts(text, data_coding)
        except SMPPPDUException:
            continue  # not representable in this coding
        except ValueError:
            raise MessageError('message needs more than 255 SMS parts')
        return data_coding
    raise MessageError('message text cannot be encoded as GSM 03.38 or UCS2')


class SMPPClient:
    """SMPP client for sending messages to SMSC."""

    def __init__(
        self,
        config: SMPPClientConfig,
        on_receipt: tp.Optional[tp.Callable[[Message], None]] = None,
    ):
        self.config = config
        self.on_receipt = on_receipt
        self.client: tp.Optional[SmppaiClient] = None
        self._inbound: tp.Optional[asyncio.Task] = None  # the bind's receipt consumer
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
            await self._drop()
            client = SmppaiClient(
                host=self.config.host,
                port=self.config.port,
                system_id=self.config.system_id,
                password=self.config.password,
                system_type=self.config.system_type,
            )
            # Wrap before the bind, so receipts the SMSC flushes right behind
            # bind_resp wait in smppai's queue instead of being acked and dropped.
            inbound = Client(client)
            try:
                await client.connect()
                await client.bind_transceiver()
            # BaseException incl. CancelledError: never abandon a half-open bind.
            except BaseException as e:
                logger.error(f'Failed to connect SMPP client: {e!r}')
                await self._close(client)
                raise

            self.client = client
            self._inbound = asyncio.create_task(self._consume(inbound))
            logger.info(
                f'SMPP client connected to {self.config.host}:{self.config.port}'
            )
            return client

    async def disconnect(self) -> None:
        """Disconnect from SMSC."""
        await self._drop()
        logger.info('SMPP client disconnected')

    async def send_message(self, message: SMSMessage) -> dict[str, tp.Any]:
        """Send SMS message via SMPP, reconnecting lazily if the bind was lost."""
        data_coding = choose_data_coding(message.message_text)
        source = Address.parse(message.source_addr)
        destination = Address.parse(message.destination_addr)

        client = await self.connect()

        try:
            # smppai splits the text and sets the UDH per part; one SMSC id per part
            smsc_message_ids = await client.submit_multipart(
                source.addr,
                destination.addr,
                message.message_text,
                data_coding=data_coding,
                registered_delivery=RegisteredDelivery.SUCCESS_FAILURE
                if message.dlr_requested
                else RegisteredDelivery.NO_RECEIPT,
                source_addr_ton=source.ton,
                source_addr_npi=source.npi,
                dest_addr_ton=destination.ton,
                dest_addr_npi=destination.npi,
            )
        except Exception as e:
            logger.error(f'Failed to send message via SMPP: {e}')
            # The whole message is retried; parts the SMSC already accepted are orphans
            sent = getattr(e, 'sent_message_ids', [])
            if sent:
                logger.warning(
                    f'Message {message.message_id}: parts already accepted as {sent}'
                )
            # An SMSC response with an error command_status came over a healthy bind;
            # anything else (timeout, I/O, not bound) drops it. Drop only the bind that
            # failed; another worker may already have rebound.
            smsc_rejected = (
                isinstance(e, SMPPException) and e.command_status is not None
            )
            if not smsc_rejected and self.client is client:
                await self._drop()
            raise

        logger.info(f'Message {message.message_id} sent via SMPP')
        return {
            'message_id': message.message_id,
            'status': 'submitted',
            'smsc_message_ids': smsc_message_ids,
        }

    async def _consume(self, inbound: Client) -> None:
        """Hand receipts (parsed by smppai) to on_receipt until the connection is lost.

        On porth's own disconnect or an SMSC unbind messages() never ends; cancelling
        this task is then its only exit.
        """
        try:
            async for msg in inbound.messages():
                if not msg.is_receipt or self.on_receipt is None:
                    # MO routing is not in MVP (design.md §2)
                    logger.info(f'Inbound message from {msg.sender} dropped')
                    continue
                try:
                    self.on_receipt(msg)
                except Exception:
                    logger.exception('Error handling delivery receipt')
        except Exception as e:
            logger.warning(f'SMPP inbound stream ended: {e!r}')

    async def _drop(self) -> None:
        """Forget the current bind: stop its receipt consumer, then close it."""
        client, self.client = self.client, None
        if self._inbound is not None:
            self._inbound.cancel()
            self._inbound = None
        await self._close(client)

    @staticmethod
    async def _close(client: tp.Optional[SmppaiClient]) -> None:
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:
            logger.error(f'Error disconnecting SMPP client: {e}')
