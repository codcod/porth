"""Delivery receipts: correlate them to messages. Final statuses: write them, and call
the Kannel dlr-url or REST status callback they are due (design.md §4.1, §4.2)."""

import asyncio
import logging
import re
import typing as tp
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
from smpp import Message, MessageState

from porth import metrics
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
_ATTEMPTS = 3  # a Kannel dlr-url GET: waits 1, 2 s
_REST_ATTEMPTS = 8  # a REST status callback POST: waits 2, 4 … 128 s
# Seconds between look-ups of a receipt's unknown SMSC id: a part's receipt can
# beat the indexing of the message's ids, which waits for every part's submit_sm_resp.
# ponytail: one task per unknown receipt, add a cap if an SMSC floods unknown ids
_UNKNOWN_WAITS = (1, 2, 4, 8)

# (message_id, url, body): a GET of url when body is None, else a POST of body
Call = tuple[str, str, dict[str, tp.Any] | None]


def expand_url(url: str, values: tp.Mapping[str, str | bytes]) -> str:
    """Substitute Kannel's escape codes (the keys of values) in one pass; `%%` is a
    literal `%`; any other %x stays as written (Kannel's rules)."""

    def code(m: re.Match) -> str:
        c = m.group(1)
        return '%' if c == '%' else quote(values[c], safe='')

    return re.sub(f'%([{re.escape("".join(values))}%])', code, url)


class DLRHandler:
    """Moves a message to its final status from receipts; every final status, whatever
    sets it, is written through finalize(), which stores the call it is due."""

    def __init__(self, uow_factory: tp.Callable[[], AbstractUnitOfWork]):
        self.uow_factory = uow_factory
        self._session: tp.Optional[aiohttp.ClientSession] = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def resume(self) -> None:
        """Make the calls a previous run (or recovery) stored but did not finish."""
        async with self.uow_factory() as uow:
            pending = await uow.messages.callbacks()
        for call in pending:
            self._spawn(self._fetch(*call))
        if pending:
            logger.info(f'Resumed {len(pending)} final-status call(s) from the store')

    async def stop(self) -> None:
        """Cancel tasks in flight; a cancelled call stays stored for resume()."""
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
        metrics.receipts.labels(smsc, 'false').inc()
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
                metrics.receipts.labels(smsc, 'true').inc()
                if message.status in _TERMINAL:
                    logger.debug(
                        f'Receipt {smsc_id} for {message.status.value} message '
                        f'{message.message_id} ignored'
                    )
                    return True
                self._advance(message, receipt.state, smsc_id, now)
                call = None
                if message.status in _TERMINAL:
                    call = await self.finalize(
                        uow, message, now, smsc_id, msg.text or ''
                    )
                else:
                    await uow.messages.update(message)
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
        # Own task, so smppai's receive loop never waits on the client's server
        self.dispatch(call)
        return True

    async def finalize(
        self,
        uow: AbstractUnitOfWork,
        message: SMSMessage,
        now: datetime,
        smsc_id: tp.Optional[str] = None,
        text: str = '',
    ) -> tp.Optional[Call]:
        """Write message's final status (set at now) in uow, with the call it is due.

        The caller commits, then passes the returned call to dispatch(). A Kannel
        dlr-url is due only when a receipt (smsc_id, text) drives the status.
        """
        assert message.status in _TERMINAL
        # ponytail: counted before the caller commits, so a rolled-back write counts
        # too; counting after commit would change every caller
        metrics.final.labels(message.smsc or '', message.status.value).inc()
        await uow.messages.update(message)
        call: tp.Optional[Call] = None
        if message.protocol == 'http' and message.callback_url:
            call = (message.message_id, message.callback_url, _rest_body(message, now))
        elif smsc_id is not None and (
            url := self._kannel_url(message, smsc_id, text, now)
        ):
            call = (message.message_id, url, None)
        if call:
            await uow.messages.add_callback(*call)
        return call

    def dispatch(self, call: tp.Optional[Call]) -> None:
        """Make a call finalize() stored, once its unit of work has committed."""
        if call:
            self._spawn(self._fetch(*call))

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
    def _kannel_url(
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

    async def _fetch(
        self, message_id: str, url: str, body: tp.Optional[dict[str, tp.Any]]
    ) -> None:
        """Make the call, then forget the stored call (at-least-once: a call
        cancelled by stop() stays stored and is made again by the next resume())."""
        await self._call(message_id, url, body)
        try:
            async with self.uow_factory() as uow:
                await uow.messages.delete_callback(message_id)
                await uow.commit()
        except Exception:
            logger.exception(
                f'{_kind(body)} for {message_id} done, but still stored: '
                'the next start calls it again'
            )

    async def _call(
        self, message_id: str, url: str, body: tp.Optional[dict[str, tp.Any]]
    ) -> None:
        """GET url (Kannel) or POST body to it (REST), retrying until a 2xx."""
        assert self._session is not None
        kind = _kind(body)
        attempts = _ATTEMPTS if body is None else _REST_ATTEMPTS
        for attempt in range(attempts):
            try:
                # Not followed: a 3xx would turn the POST into a body-less GET whose
                # 2xx counts as delivered, so it is retried like any non-2xx
                request = (
                    self._session.get(url)
                    if body is None
                    else self._session.post(url, json=body, allow_redirects=False)
                )
                async with request as response:
                    if 200 <= response.status < 300:
                        return
                    error = f'HTTP {response.status}'
            except Exception as e:  # client-supplied URL: anything can go wrong
                error = repr(e)
            logger.warning(
                f'{kind} for {message_id}, attempt {attempt + 1}/{attempts}: {error}'
            )
            if attempt + 1 < attempts:
                await asyncio.sleep(2 ** (attempt if body is None else attempt + 1))
        logger.error(f'{kind} for {message_id} gave up after {attempts} attempts')


def _kind(body: tp.Optional[dict[str, tp.Any]]) -> str:
    return 'dlr-url' if body is None else 'status callback'


def _rest_body(message: SMSMessage, now: datetime) -> dict[str, tp.Any]:
    """The REST status callback's body (design.md §4.1); now is UTC."""
    body = {
        'message_id': message.message_id,
        'status': message.status.value,
        'occurred_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if message.idempotency_key:
        body['idempotency_key'] = message.idempotency_key
    return body
