"""Delivery receipts: correlate them to messages, fetch Kannel dlr-urls (design.md §4.2)."""

import asyncio
import logging
import re
import typing as tp
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
from smpp import Message, MessageState

from porth.core.message import MessageStatus, SMSMessage
from porth.core.store import MessageStore

logger = logging.getLogger(__name__)

_FAILED = {MessageState.UNDELIVERABLE, MessageState.REJECTED, MessageState.DELETED}
_TERMINAL = {MessageStatus.DELIVERED, MessageStatus.FAILED, MessageStatus.EXPIRED}
# Kannel dlr-mask bit, which is also the %d value, per outcome
_DLR_BIT = {
    MessageStatus.DELIVERED: 1,
    MessageStatus.FAILED: 2,
    MessageStatus.EXPIRED: 2,
}
_ATTEMPTS = 3
# Seconds between look-ups of a receipt's unknown SMSC id: a part's receipt can
# beat the indexing of the message's ids, which waits for every part's submit_sm_resp.
# ponytail: one task per unknown receipt, add a cap if an SMSC floods unknown ids
_UNKNOWN_WAITS = (1, 2, 4, 8)


def expand_dlr_url(url: str, values: dict[str, str]) -> str:
    """Substitute Kannel's %d %I %F %A %t %T in one pass; `%%` is a literal `%`; any
    other %x stays as written (Kannel's rules)."""

    def code(m: re.Match) -> str:
        c = m.group(1)
        return '%' if c == '%' else quote(values[c], safe='')

    return re.sub(r'%([dIFAtT%])', code, url)


class DLRHandler:
    """Moves a message to its final status from receipts; calls its Kannel dlr-url."""

    def __init__(self, message_store: MessageStore):
        self.message_store = message_store
        self._session: tp.Optional[aiohttp.ClientSession] = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def stop(self) -> None:
        if self._session is None:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._session.close()
        self._session = None

    def on_receipt(self, msg: Message) -> None:
        """Apply one parsed receipt (smppai's) to the message it reports on."""
        assert msg.receipt is not None
        now = datetime.now(timezone.utc)
        if self._apply(msg, now):
            return
        if not msg.receipt.id:
            self._ignore(msg)
            return
        self._spawn(self._recheck(msg, now))

    async def _recheck(self, msg: Message, now: datetime) -> None:
        for wait in _UNKNOWN_WAITS:
            await asyncio.sleep(wait)
            if self._apply(msg, now):
                return
        self._ignore(msg)

    @staticmethod
    def _ignore(msg: Message) -> None:
        assert msg.receipt is not None
        # info, not debug: a systematic SMSC id-format mismatch must show up
        logger.info(f'Receipt for unknown SMSC id {msg.receipt.id!r} ignored')

    def _apply(self, msg: Message, now: datetime) -> bool:
        """Apply the receipt to its message; False if its SMSC id is unknown."""
        receipt = msg.receipt
        assert receipt is not None
        smsc_id = receipt.id
        message = self.message_store.find_by_smsc_id(smsc_id) if smsc_id else None
        if message is None or smsc_id is None:
            return False
        if message.status in _TERMINAL:
            logger.debug(
                f'Receipt {receipt.id} for {message.status.value} message '
                f'{message.message_id} ignored'
            )
            return True

        if receipt.state == MessageState.DELIVERED:
            delivered = message.protocol_data.setdefault('delivered_smsc_ids', set())
            delivered.add(smsc_id)
            if not delivered.issuperset(message.protocol_data['smsc_message_ids']):
                return True  # other parts still outstanding
            message.status = MessageStatus.DELIVERED
            message.delivered_at = now.replace(tzinfo=None)  # naive UTC, like sent_at
        elif receipt.state == MessageState.EXPIRED:
            message.status = MessageStatus.EXPIRED
        elif receipt.state in _FAILED:
            message.status = MessageStatus.FAILED
        else:
            logger.info(
                f'Receipt {receipt.id} for message {message.message_id}: '
                f'non-final state {receipt.state!r}'
            )
            return True

        logger.info(f'Message {message.message_id} {message.status.value} (receipt)')
        self._callback(message, smsc_id, msg.text or '', now)
        return True

    def _callback(
        self, message: SMSMessage, smsc_id: str, text: str, now: datetime
    ) -> None:
        bit = _DLR_BIT[message.status]
        if (
            message.protocol != 'kannel'
            or not message.dlr_url
            or not message.protocol_data.get('dlr_mask', 0) & bit
        ):
            return
        url = expand_dlr_url(
            message.dlr_url,
            {
                'd': str(bit),
                'I': message.message_id,
                'F': smsc_id,
                'A': text,
                't': now.strftime('%Y-%m-%d %H:%M'),
                'T': str(int(now.timestamp())),
            },
        )
        # Own task, so smppai's receive loop never waits on the client's server
        self._spawn(self._fetch(message.message_id, url))

    def _spawn(self, coro: tp.Coroutine[tp.Any, tp.Any, None]) -> None:
        """Run coro as a task that stop() cancels."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch(self, message_id: str, url: str) -> None:
        assert self._session is not None
        for attempt in range(_ATTEMPTS):
            try:
                async with self._session.get(url) as response:
                    if 200 <= response.status < 300:
                        return
                    error = f'HTTP {response.status}'
            except Exception as e:  # client-supplied URL: anything can go wrong
                error = repr(e)
            logger.warning(
                f'dlr-url for {message_id}, attempt {attempt + 1}/{_ATTEMPTS}: {error}'
            )
            if attempt + 1 < _ATTEMPTS:
                await asyncio.sleep(2**attempt)
        logger.error(f'dlr-url for {message_id} gave up after {_ATTEMPTS} attempts')
