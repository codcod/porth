"""Async message queue for SMS messages."""

import asyncio
import logging
import typing as tp
from porth.core.message import SMSMessage

logger = logging.getLogger(__name__)


class MessageQueue:
    """Async message queue for SMS messages."""

    def __init__(self, maxsize: int = 0):
        self._queue: asyncio.Queue[SMSMessage] = asyncio.Queue(maxsize=maxsize)
        self._running = False

    async def put(self, message: SMSMessage) -> None:
        """Add a message to the queue."""
        try:
            await self._queue.put(message)
            logger.debug(f'Message {message.message_id} added to queue')
        except asyncio.QueueFull:
            logger.error(f'Queue full, cannot add message {message.message_id}')
            raise

    async def get(self, timeout: tp.Optional[float] = None) -> SMSMessage:
        """Get a message from the queue."""
        try:
            message = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            logger.debug(f'Message {message.message_id} retrieved from queue')
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
