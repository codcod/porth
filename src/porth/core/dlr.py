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
from porth.service_layer.unit_of_work import AbstractUnitOfWork

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


def expand_url(url: str, values: tp.Mapping[str, str | bytes]) -> str:
    """Substitute Kannel's escape codes (the keys of values) in one pass; `%%` is a
    literal `%`; any other %x stays as written (Kannel's rules)."""

    def code(m: re.Match) -> str:
        c = m.group(1)
        return '%' if c == '%' else quote(values[c], safe='')

    return re.sub(f'%([{re.escape("".join(values))}%])', code, url)


class DLRHandler:
    """Moves a message to its final status from receipts; calls its Kannel dlr-url."""

    def __init__(self, uow_factory: tp.Callable[[], AbstractUnitOfWork]):
        self.uow_factory = uow_factory
        self._session: tp.Optional[aiohttp.ClientSession] = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def resume(self) -> None:
        """Make the dlr-url calls a previous run stored but did not finish."""
        async with self.uow_factory() as uow:
            pending = await uow.messages.callbacks()
        for message_id, url in pending:
            self._spawn(self._fetch(message_id, url))
        if pending:
            logger.info(f'Resumed {len(pending)} dlr-url call(s) from the store')

    async def stop(self) -> None:
        """Cancel tasks in flight; a cancelled dlr-url call stays stored for resume()."""
        if self._session is None:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._session.close()
        self._session = None

    async def on_receipt(self, msg: Message, smsc: str) -> None:
        """Apply one parsed receipt (smppai's), from the SMSC named smsc, to the
        message it reports on.

        Awaited by the receive loop, so a receipt is committed before its bind lets
        go of it.
        """
        assert msg.receipt is not None
        now = datetime.now(timezone.utc)
        if await self._apply(msg, smsc, now):
            return
        if not msg.receipt.id:
            self._ignore(msg, smsc)
            return
        self._spawn(self._recheck(msg, smsc, now))

    # ponytail: re-checks live in memory; a crash inside the ~15 s window loses the
    # receipt (design.md §7 item 1), persist unmatched receipts if that bites
    async def _recheck(self, msg: Message, smsc: str, now: datetime) -> None:
        for wait in _UNKNOWN_WAITS:
            await asyncio.sleep(wait)
            if await self._apply(msg, smsc, now):
                return
        self._ignore(msg, smsc)

    @staticmethod
    def _ignore(msg: Message, smsc: str) -> None:
        assert msg.receipt is not None
        # info, not debug: a systematic SMSC id-format mismatch must show up
        logger.info(
            f'Receipt for unknown SMSC id {msg.receipt.id!r} from {smsc} ignored'
        )

    async def _apply(self, msg: Message, smsc: str, now: datetime) -> bool:
        """Apply the receipt to its message in one transaction; False if its SMSC id
        is unknown. A database error is logged and counts as applied (lost)."""
        receipt = msg.receipt
        assert receipt is not None
        smsc_id = receipt.id
        if not smsc_id:
            return False
        try:
            async with self.uow_factory() as uow:
                # The row lock serialises parts of one message across the receive
                # loop and the re-check tasks
                message = await uow.messages.get_by_smsc_id_for_update(smsc, smsc_id)
                if message is None:
                    return False
                if message.status in _TERMINAL:
                    logger.debug(
                        f'Receipt {smsc_id} for {message.status.value} message '
                        f'{message.message_id} ignored'
                    )
                    return True
                self._advance(message, receipt.state, smsc_id, now)
                url = (
                    self._callback_url(message, smsc_id, msg.text or '', now)
                    if message.status in _TERMINAL
                    else None
                )
                await uow.messages.update(message)
                if url:
                    await uow.messages.add_callback(message.message_id, url)
                await uow.commit()
        except Exception:
            logger.exception(
                f'Receipt {smsc_id!r} ({receipt.state!r}) not saved, so lost'
            )
            return True

        if message.status in _TERMINAL:
            logger.info(
                f'Message {message.message_id} {message.status.value} (receipt)'
            )
        if url:
            # Own task, so smppai's receive loop never waits on the client's server
            self._spawn(self._fetch(message.message_id, url))
        return True

    @staticmethod
    def _advance(
        message: SMSMessage,
        state: tp.Optional[MessageState],
        smsc_id: str,
        now: datetime,
    ) -> None:
        """Move a non-final message along by one receipt."""
        if state == MessageState.DELIVERED:
            delivered = message.protocol_data.setdefault('delivered_smsc_ids', [])
            if smsc_id not in delivered:
                delivered.append(smsc_id)
            if set(delivered).issuperset(message.protocol_data['smsc_message_ids']):
                message.status = MessageStatus.DELIVERED
                message.delivered_at = now
            # else other parts still outstanding
        elif state == MessageState.EXPIRED:
            message.status = MessageStatus.EXPIRED
        elif state in _FAILED:
            message.status = MessageStatus.FAILED
        else:
            logger.info(
                f'Receipt {smsc_id} for message {message.message_id}: '
                f'non-final state {state!r}'
            )

    @staticmethod
    def _callback_url(
        message: SMSMessage, smsc_id: str, text: str, now: datetime
    ) -> tp.Optional[str]:
        """The expanded Kannel dlr-url for this final status, or None if none is due."""
        bit = _DLR_BIT[message.status]
        if (
            message.protocol != 'kannel'
            or not message.dlr_url
            or not message.protocol_data.get('dlr_mask', 0) & bit
        ):
            return None
        return expand_url(
            message.dlr_url,
            {
                'd': str(bit),
                'I': message.message_id,
                'F': smsc_id,
                'A': text,
                't': now.strftime('%Y-%m-%d %H:%M'),
                'T': str(int(now.timestamp())),
                'i': message.smsc or '',
            },
        )

    def _spawn(self, coro: tp.Coroutine[tp.Any, tp.Any, None]) -> None:
        """Run coro as a task that stop() cancels."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch(self, message_id: str, url: str) -> None:
        """Call the dlr-url, then forget the stored call (at-least-once: a call
        cancelled by stop() stays stored and is made again by the next resume())."""
        await self._call(message_id, url)
        try:
            async with self.uow_factory() as uow:
                await uow.messages.delete_callback(message_id)
                await uow.commit()
        except Exception:
            logger.exception(
                f'dlr-url for {message_id} done, but still stored: '
                'the next start calls it again'
            )

    async def _call(self, message_id: str, url: str) -> None:
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
