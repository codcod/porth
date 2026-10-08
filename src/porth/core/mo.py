"""MO SMS: forward to the application's Kannel get-url, queue its reply (design.md §4.4)."""

import asyncio
import logging
import typing as tp
from datetime import datetime, timezone

import aiohttp
from smpp import Address, DataCoding, Message, TonType

from porth import metrics
from porth.config.settings import MOConfig
from porth.core.dlr import expand_url
from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from porth.service_layer.unit_of_work import AbstractUnitOfWork

logger = logging.getLogger(__name__)

# ponytail: fixed 30 s, make it a setting if an application needs longer
_TIMEOUT = 30


def number(address: Address) -> str:
    """The address as Kannel writes it: a leading '+' for a TON-international number."""
    if address.ton == TonType.INTERNATIONAL:
        return '+' + address.addr.lstrip('+')
    return address.addr


def mo_values(msg: Message, now: datetime) -> dict[str, str | bytes]:
    """Kannel get-url escape codes for one MO, byte-for-byte as Kannel 1.4.5 sends them.

    Kannel hands the application UTF-8 for GSM text and raw UTF-16BE for UCS-2 (the
    HTTP contract, not an SMPP codec), and splits words on whitespace bytes.
    """
    assert msg.text is not None
    # ponytail: two codings only, anything else is sent as UTF-8 text; add %c values
    # beyond 0/2 when an operator sends 8-bit MO
    if msg.pdu.data_coding == DataCoding.UCS2:
        data, coding, charset = msg.text.encode('utf-16-be'), '2', 'UTF-16BE'
    else:
        data, coding, charset = msg.text.encode('utf-8'), '0', 'UTF-8'
    words = data.split()
    return {
        'p': number(msg.sender),
        'P': number(msg.to),
        'k': words[0] if words else b'',
        'r': b' '.join(words[1:]),
        'a': b' '.join(words),
        'b': data,
        't': now.strftime('%Y-%m-%d %H:%M:%S'),
        'T': str(int(now.timestamp())),
        'c': coding,
        'C': charset,
    }


class MOHandler:
    """Fetches the MO URL once per MO (at-most-once) and queues a text/plain reply."""

    def __init__(
        self,
        queues: tp.Mapping[str, MessageQueue],
        uow_factory: tp.Callable[[], AbstractUnitOfWork],
        config: MOConfig,
    ):
        self.queues = queues  # SMSC name -> its engine's queue
        self.uow_factory = uow_factory
        self.config = config
        self._session: tp.Optional[aiohttp.ClientSession] = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT)
        )

    async def stop(self) -> None:
        """Cancel requests in flight (their MOs are lost) and close the session."""
        if self._session is None:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._session.close()
        self._session = None

    def on_mo(self, msg: Message, smsc: str) -> None:
        """Forward one MO (smppai's, reassembled), from the SMSC named smsc, in its
        own task."""
        if not self.config.url:
            logger.info(f'MO from {msg.sender.addr} dropped: mo.url is not set')
            return
        if msg.text is None:
            logger.info(f'MO from {msg.sender.addr} dropped: text not decodable')
            return
        values = mo_values(msg, datetime.now(timezone.utc))
        values['i'] = smsc
        url = expand_url(self.config.url, values)
        # Own task, so smppai's receive loop never waits on the application
        task = asyncio.create_task(self._forward(msg, smsc, url))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _forward(self, msg: Message, smsc: str, url: str) -> None:
        assert self._session is not None
        sender, body = msg.sender.addr, ''
        try:
            async with self._session.get(url) as response:
                if response.status not in (200, 202):  # Kannel takes both
                    why = f'HTTP {response.status}'
                elif response.content_type != 'text/plain':
                    why = f'content type {response.content_type}'
                elif not self.config.reply:
                    why = 'mo.reply is off'
                else:
                    # Kannel strips blanks, so a whitespace-only body is empty
                    body = (await response.text()).strip()
                    why = '' if body else 'empty body'
        except Exception as e:  # operator-supplied URL: anything can go wrong
            logger.warning(f'MO from {sender}: application request failed: {e!r}')
            metrics.mo.labels(smsc, 'failed').inc()
            return
        metrics.mo.labels(smsc, 'forwarded').inc()
        if why:
            logger.info(f'MO from {sender}: no reply sent ({why})')
            return
        reply = SMSMessage(
            source_addr=number(msg.to),
            destination_addr=number(msg.sender),
            message_text=body,
            protocol='kannel',
            protocol_data={'dlr_mask': 0},
            dlr_requested=False,
            status=MessageStatus.QUEUED,
            # The SMSC the MO arrived on, not routed (design.md 1.28)
            smsc=smsc,
        )
        # Durable before queueing, as the submit handlers do
        try:
            async with self.uow_factory() as uow:
                await uow.messages.add(reply)
                await uow.commit()
        except Exception as e:
            logger.warning(f'MO from {sender}: reply not stored, not sent: {e!r}')
            return
        await self.queues[smsc].put(reply)
        logger.info(f'MO from {sender}: reply {reply.message_id} queued')
