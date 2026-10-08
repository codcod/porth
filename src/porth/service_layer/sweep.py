"""The Message Store sweep: expire messages whose receipt never came, delete finished
ones past the retention window (design.md §3.3, §5)."""

import asyncio
import logging
import typing as tp
from datetime import datetime, timedelta, timezone

from porth.config.settings import StoreConfig
from porth.service_layer.dlr import DLRHandler
from porth.domain.model import MessageStatus
from porth.service_layer.unit_of_work import AbstractUnitOfWork

logger = logging.getLogger(__name__)

# ponytail: fixed 60 s, so an expiry lands up to a minute late; a setting if that matters
_INTERVAL = 60
# ponytail: per-pass caps keep each transaction short; a backlog drains over later passes
_EXPIRE_LIMIT = 1_000
_EVICT_LIMIT = 10_000


class Sweeper:
    """Runs sweep_once() right after start(), then every _INTERVAL seconds."""

    def __init__(
        self,
        uow_factory: tp.Callable[[], AbstractUnitOfWork],
        dlr_handler: DLRHandler,
        config: StoreConfig,
    ):
        self.uow_factory = uow_factory
        self.dlr_handler = dlr_handler
        self.config = config
        self._task: tp.Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep_once(datetime.now(timezone.utc))
            except Exception:
                logger.exception('Message store sweep failed; retrying next pass')
            await asyncio.sleep(_INTERVAL)

    async def sweep_once(self, now: datetime) -> None:
        """One pass: expire, then evict, each in its own unit of work."""
        timeout = timedelta(hours=self.config.dlr_timeout_hours)
        async with self.uow_factory() as uow:
            calls = []
            expired = await uow.messages.unreceipted(now - timeout, _EXPIRE_LIMIT)
            for message in expired:
                message.status = MessageStatus.EXPIRED
                calls.append(await self.dlr_handler.finalize(uow, message, now))
            await uow.commit()
        for call in calls:
            self.dlr_handler.dispatch(call)

        retention = timedelta(days=self.config.retention_days)
        async with self.uow_factory() as uow:
            evicted = await uow.messages.evict(now - retention, _EVICT_LIMIT)
            await uow.commit()

        if expired or evicted:
            logger.info(
                f'Message store sweep: {len(expired)} expired (no receipt), '
                f'{evicted} deleted'
            )
