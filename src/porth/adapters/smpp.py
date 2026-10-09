"""SMPP client implementation using smppai."""

import asyncio
import collections
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
from smpp.gsm import SMPP_GSMFEAT_UDHI, make_parts
from smpp.protocol.codec import encode_message_with_encoding
from smpp.utils import format_smpp_time

from porth import metrics
from porth.config.settings import SMPPClientConfig
from porth.domain.exceptions import MessageError
from porth.domain.model import SMSMessage

logger = logging.getLogger(__name__)

# Kannel's reconnect-delay default.
# ponytail: fixed 10 s, make it a setting if an operator needs another
_REBIND_DELAY = 10.0


def choose_data_coding(text: str) -> tuple[DataCoding, int]:
    """
    GSM 03.38 if smppai's codec can carry text, else UCS2 (as smppai's Client.send
    picks), with the number of parts smppai's segmentation sends it as. Raise
    MessageError if that needs more than 255 parts.
    """
    for data_coding in (DataCoding.DEFAULT, DataCoding.UCS2):
        try:
            parts = make_parts(text, data_coding)
        except SMPPPDUException:
            continue  # not representable in this coding
        except ValueError:
            raise MessageError('message needs more than 255 SMS parts')
        return data_coding, len(parts)
    raise MessageError('message text cannot be encoded as GSM 03.38 or UCS2')


def sms_payload(
    content: str | bytes, data_coding: int, udh: tp.Optional[bytes]
) -> str | bytes:
    """
    What submit_multipart sends: text content itself, which smppai splits on
    characters, unless it carries a client UDH. Then bytes: the UDH and the text
    encoded with data_coding. 8-bit content is bytes already.
    """
    if isinstance(content, str):
        if udh is None:
            return content
        content = encode_message_with_encoding(content, data_coding)
    return (udh or b'') + content


class Pacer:
    """At most `rate` PDUs in any one-second window, sends spaced `n / rate` apart."""

    def __init__(self, rate: int):
        self.rate = rate
        self.next_at = 0.0
        # ponytail: `rate` floats per client, fine at any contracted TPS
        self.log: collections.deque[float] = collections.deque(maxlen=rate)

    def reserve(self, n: int, now: float) -> float:
        """Book a send of `n` PDUs at `now`; return the seconds to wait before it."""
        start = max(now, self.next_at)
        # the one second before start must have room for all n parts (at most rate)
        k = len(self.log) + min(n, self.rate) - self.rate
        if k > 0:
            start = max(start, self.log[k - 1] + 1)
        self.next_at = start + n / self.rate
        self.log.extend([start] * n)
        return start - now


class SMPPClient:
    """SMPP client for sending messages to SMSC."""

    def __init__(
        self,
        name: str,
        config: SMPPClientConfig,
        on_receipt: tp.Optional[tp.Callable[[Message], tp.Awaitable[None]]] = None,
        on_mo: tp.Optional[tp.Callable[[Message], None]] = None,
    ):
        throughput = config.throughput
        if throughput is not None and throughput < 1:
            raise ValueError(
                f'invalid porth.smsc.{name}.throughput: must be at least 1'
            )
        self.name = name
        self.config = config
        self._pacer = Pacer(throughput) if throughput is not None else None
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
                logger.error(f'SMSC {self.name}: Failed to connect SMPP client: {e!r}')
                raise

            self._bind = stack
            self.client = inbound.raw
            self._inbound = asyncio.create_task(self._consume(inbound))
            logger.info(
                f'SMSC {self.name}: SMPP client connected to '
                f'{self.config.host}:{self.config.port}'
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
        logger.info(f'SMSC {self.name}: SMPP client disconnected')

    async def send_message(self, message: SMSMessage) -> dict[str, tp.Any]:
        """Send SMS message via SMPP, reconnecting lazily if the bind was lost."""
        payload: str | bytes = message.message_text
        esm_class = 0
        if 'data_coding' in message.protocol_data:  # chosen at submit (Kannel's)
            data_coding = message.protocol_data['data_coding']
            udh = message.protocol_data.get('udh')
            udh = bytes.fromhex(udh) if udh is not None else None
            data = message.protocol_data.get('data')  # 8-bit content
            content = bytes.fromhex(data) if data is not None else message.message_text
            payload = sms_payload(content, data_coding, udh)
            parts = len(make_parts(payload, data_coding))
            if udh is not None:
                # the client's UDH: one segment, checked at submit
                esm_class = SMPP_GSMFEAT_UDHI
        else:
            data_coding, parts = choose_data_coding(message.message_text)
        source = Address.parse(message.source_addr)
        destination = Address.parse(message.destination_addr)

        client = await self.connect()

        if self._pacer is not None:
            # after the bind: sends that waited out a slow rebind still go out spaced.
            # A bind lost during the wait is rebound, and the send books a fresh turn
            # instead of failing on the dead one and spending a retry.
            loop = asyncio.get_running_loop()
            while True:
                await asyncio.sleep(self._pacer.reserve(parts, loop.time()))
                if self.client is client and client.is_bound:
                    break
                client = await self.connect()

        try:
            # smppai splits the text and sets the UDH per part; one SMSC id per part
            with metrics.submit_seconds.labels(self.name).time():
                smsc_message_ids = await client.submit_multipart(
                    source.addr,
                    destination.addr,
                    payload,
                    data_coding=data_coding,
                    esm_class=esm_class,
                    registered_delivery=RegisteredDelivery.SUCCESS_FAILURE
                    if message.dlr_requested
                    else RegisteredDelivery.NO_RECEIPT,
                    source_addr_ton=source.ton,
                    source_addr_npi=source.npi,
                    dest_addr_ton=destination.ton,
                    dest_addr_npi=destination.npi,
                    # the SMSC drops it past then too (design.md §4.2); '': its default
                    validity_period=format_smpp_time(message.valid_until.timestamp())
                    if message.valid_until
                    else '',
                )
        except Exception as e:
            logger.error(f'SMSC {self.name}: Failed to send message via SMPP: {e}')
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
                    logger.info(
                        f'SMSC {self.name}: Inbound {kind} from {msg.sender} dropped'
                    )
                    continue
                try:
                    result = handler(msg)
                    if result is not None:  # on_receipt's: the receipt is stored
                        await result
                except Exception:
                    logger.exception(f'SMSC {self.name}: Error handling {kind}')
        except Exception as e:
            logger.warning(f'SMSC {self.name}: SMPP inbound stream ended: {e!r}')

    async def _drop(self) -> None:
        """Forget the current bind: close it, then let its consumer drain its receipts."""
        stack, consumer = self._bind, self._inbound
        self._bind = self.client = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as e:
                logger.error(f'SMSC {self.name}: Error disconnecting SMPP client: {e}')
        if consumer is not None:
            # shield: a cancelled caller (disconnect() stopping the rebind loop)
            # must not cancel the drain; the consumer ends on smppai's end marker.
            # _inbound is cleared only once it has, so disconnect()'s own _drop()
            # still waits for a drain the cancelled loop left running.
            await asyncio.shield(consumer)
            if self._inbound is consumer:
                self._inbound = None
