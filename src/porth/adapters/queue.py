"""Async message queue for SMS messages."""

import asyncio
import itertools
import logging
import typing as tp
from porth.domain.model import MessageStatus, SMSMessage

logger = logging.getLogger(__name__)


def _fields(message: SMSMessage) -> dict[str, tp.Any]:
    return {'message_id': message.message_id, 'smsc': message.smsc}


class MessageQueue:
    """Async message queue for SMS messages: high priority first, FIFO within a level."""

    def __init__(self, limit: tp.Optional[int] = None):
        # Unbounded on purpose: put never blocks, so retries and accepted submits always
        # get on; `limit` only answers `full()`, which services.submit checks before it
        # stores anything (an MO reply included: a full queue drops it)
        self._queue: asyncio.PriorityQueue[tuple[int, int, SMSMessage]] = (
            asyncio.PriorityQueue()
        )
        self._seq = itertools.count()
        self._running = False
        self._limit = limit
        self._timers: set[asyncio.Task] = set()

    async def put(self, message: SMSMessage) -> None:
        """Add a message to the queue."""
        message.status = MessageStatus.QUEUED
        level = 0 if message.priority == 'high' else 1
        await self._queue.put((level, next(self._seq), message))
        logger.debug(
            f'Message {message.message_id} added to queue', extra=_fields(message)
        )

    def put_later(self, message: SMSMessage, delay: float) -> None:
        """Put message on the queue after delay seconds; it counts as retrying until then."""
        task = asyncio.create_task(self._put_later(message, delay))
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    async def _put_later(self, message: SMSMessage, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.put(message)

    def retrying(self) -> int:
        """Messages waiting out a retry delay."""
        return len(self._timers)

    def full(self, priority: str, adding: int = 1) -> bool:
        """True when `adding` more messages would take the backlog (waiting plus retrying)
        past the limit for priority. High priority is refused only at twice the limit.
        """
        if self._limit is None:
            return False
        limit = self._limit * (2 if priority == 'high' else 1)
        return self.qsize() + self.retrying() + adding > limit

    async def cancel_retries(self) -> None:
        """Cancel every pending retry (the store keeps those messages queued)."""
        tasks = list(self._timers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def get(self, timeout: tp.Optional[float] = None) -> SMSMessage:
        """Get a message from the queue."""
        try:
            *_, message = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            logger.debug(
                f'Message {message.message_id} retrieved from queue',
                extra=_fields(message),
            )
            return message
        except asyncio.TimeoutError:
            raise

    def task_done(self) -> None:
        """Mark a task as done."""
        self._queue.task_done()

    async def join(self) -> None:
        """Wait until all tasks are done."""
        await self._queue.join()

    def qsize(self) -> int:
        """Return the queue size."""
        return self._queue.qsize()

    def empty(self) -> bool:
        """Return True if queue is empty."""
        return self._queue.empty()
