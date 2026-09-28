"""SMPP client implementation using smppai."""

import asyncio
import contextlib
import logging
import typing as tp

import smpp
from smpp import (
    Address,
    Client,
    CommandStatus,
    DataCoding,
    Message,
    RegisteredDelivery,
)
from smpp import SMPPClient as SmppaiClient
from smpp.exceptions import SMPPException, SMPPPDUException
from smpp.gsm import make_parts

from porth.config.settings import SMPPClientConfig
from porth.core.exceptions import MessageError
from porth.core.message import SMSMessage

logger = logging.getLogger(__name__)

# Kannel's reconnect-delay default.
# ponytail: fixed 10 s, make it a setting if an operator needs another
_REBIND_DELAY = 10.0


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
        on_receipt: tp.Optional[tp.Callable[[Message], tp.Awaitable[None]]] = None,
        on_mo: tp.Optional[tp.Callable[[Message], None]] = None,
    ):
        self.config = config
        self.on_receipt = on_receipt
        self.on_mo = on_mo
        self.client: tp.Optional[SmppaiClient] = None
        self._bind: tp.Optional[contextlib.AsyncExitStack] = None  # smpp.connect()'s
        self._inbound: tp.Optional[asyncio.Task] = None  # the bind's inbound consumer
        self._keeper: tp.Optional[asyncio.Task] = None  # the rebind loop
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
            stack = contextlib.AsyncExitStack()
            try:
                # smpp.connect() wraps before the bind, so receipts the SMSC flushes
                # right behind bind_resp wait in smppai's queue instead of being
                # acked and dropped; it closes a bind that fails or is cancelled.
                inbound = await stack.enter_async_context(
                    smpp.connect(
                        self.config.host,
                        self.config.port,
                        self.config.system_id,
                        self.config.password,
                        bind='trx',
                        system_type=self.config.system_type,
                    )
                )
            # BaseException incl. CancelledError: log it, smppai has closed the bind.
            except BaseException as e:
                logger.error(f'Failed to connect SMPP client: {e!r}')
                raise

            self._bind = stack
            self.client = inbound.raw
            self._inbound = asyncio.create_task(self._consume(inbound))
            logger.info(
                f'SMPP client connected to {self.config.host}:{self.config.port}'
            )
            return inbound.raw

    def start(self) -> None:
        """Keep the bind up in the background, so receipts arrive without sends."""
        self._keeper = asyncio.create_task(self._keep_bound())

    async def _keep_bound(self) -> None:
        # Not a health monitor: it sends nothing while bound (smppai's enquire_link does)
        while True:
            if not self.connected:
                with contextlib.suppress(Exception):  # connect() logged it
                    await self.connect()
            await asyncio.sleep(_REBIND_DELAY)

    async def disconnect(self) -> None:
        """Stop the rebind loop, then close the bind, draining its receipts."""
        if self._keeper is not None:
            self._keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keeper
            self._keeper = None
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
            # An SMSC response with an error command_status came over a healthy bind,
            # unless it says the SMSC lost our session; anything else (timeout, I/O,
            # not bound) drops it. Drop only the bind that failed; another worker may
            # already have rebound.
            smsc_rejected = (
                isinstance(e, SMPPException)
                and e.command_status is not None
                and e.command_status != CommandStatus.ESME_RINVBNDSTS
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
        """Hand receipts (parsed by smppai) to on_receipt, awaited, and MO
        (reassembled by smppai) to on_mo until the bind ends.

        Ends on a lost connection (the loss exception, logged) or once _drop() has
        closed the bind (after every message still queued).
        """
        try:
            async for msg in inbound.messages():
                kind, handler = (
                    ('delivery receipt', self.on_receipt)
                    if msg.is_receipt
                    else ('MO', self.on_mo)
                )
                if handler is None:
                    logger.info(f'Inbound {kind} from {msg.sender} dropped')
                    continue
                try:
                    result = handler(msg)
                    if result is not None:  # on_receipt's: the receipt is stored
                        await result
                except Exception:
                    logger.exception(f'Error handling {kind}')
        except Exception as e:
            logger.warning(f'SMPP inbound stream ended: {e!r}')

    async def _drop(self) -> None:
        """Forget the current bind: close it, then let its consumer drain its receipts."""
        stack, consumer = self._bind, self._inbound
        self._bind = self.client = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as e:
                logger.error(f'Error disconnecting SMPP client: {e}')
        if consumer is not None:
            # shield: a cancelled caller (disconnect() stopping the rebind loop)
            # must not cancel the drain; the consumer ends on smppai's end marker.
            # _inbound is cleared only once it has, so disconnect()'s own _drop()
            # still waits for a drain the cancelled loop left running.
            await asyncio.shield(consumer)
            if self._inbound is consumer:
                self._inbound = None
