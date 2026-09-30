"""Fakes for the unit of work (decision 14): dicts in place of PostgreSQL."""

import copy
import typing as tp

import pytest
from sqlalchemy.exc import IntegrityError

from porth.adapters.repository import AbstractMessageRepository
from porth.core.message import MessageStatus, SMSMessage
from porth.service_layer.unit_of_work import AbstractUnitOfWork


class FakeMessageRepository(AbstractMessageRepository):
    """Stores copies, as a database does: only what is written is kept."""

    def __init__(self) -> None:
        self.messages: dict[str, SMSMessage] = {}
        self.smsc_ids: dict[tuple[str, str], str] = {}  # (smsc, smsc_id) -> id
        self.dlr_callbacks: dict[str, str] = {}

    @tp.override
    async def add(self, item: SMSMessage) -> None:
        if item.message_id in self.messages:
            raise IntegrityError('INSERT', None, Exception('duplicate message_id'))
        self.messages[item.message_id] = copy.deepcopy(item)

    @tp.override
    async def get(self, id: str) -> SMSMessage | None:
        return copy.deepcopy(self.messages.get(id))

    @tp.override
    async def update(self, message: SMSMessage) -> None:
        stored = self.messages[message.message_id]
        for field in ('status', 'retry_count', 'sent_at', 'delivered_at', 'smsc'):
            setattr(stored, field, getattr(message, field))
        stored.protocol_data = copy.deepcopy(message.protocol_data)

    @tp.override
    async def add_smsc_ids(self, message_id: str, smsc: str, ids: list[str]) -> None:
        self.smsc_ids.update(dict.fromkeys(((smsc, i) for i in ids), message_id))

    @tp.override
    async def get_by_smsc_id_for_update(
        self, smsc: str, smsc_id: str
    ) -> SMSMessage | None:
        message_id = self.smsc_ids.get((smsc, smsc_id))
        return await self.get(message_id) if message_id else None

    @tp.override
    async def unsent(self) -> list[SMSMessage]:
        unsent = (MessageStatus.PENDING, MessageStatus.QUEUED)
        found = [m for m in self.messages.values() if m.status in unsent]
        return copy.deepcopy(sorted(found, key=lambda m: m.created_at))

    @tp.override
    async def add_callback(self, message_id: str, url: str) -> None:
        if message_id in self.dlr_callbacks:
            raise IntegrityError('INSERT', None, Exception('duplicate message_id'))
        self.dlr_callbacks[message_id] = url

    @tp.override
    async def delete_callback(self, message_id: str) -> None:
        self.dlr_callbacks.pop(message_id, None)

    @tp.override
    async def callbacks(self) -> list[tuple[str, str]]:
        return list(self.dlr_callbacks.items())


class FakeUnitOfWork(AbstractUnitOfWork):
    def __init__(self, messages: FakeMessageRepository, fail: Exception | None):
        self.messages = messages
        self.fail = fail
        self.committed = False

    def _state(self) -> tuple[dict[str, tp.Any], ...]:
        return (
            self.messages.messages,
            self.messages.smsc_ids,
            self.messages.dlr_callbacks,
        )

    @tp.override
    async def __aenter__(self) -> tp.Self:
        # ponytail: whole-state snapshot, exact while no other unit of work writes
        # during this one's body (the fakes never yield); per-key undo if tests need it
        self._snapshot = copy.deepcopy(self._state())
        return await super().__aenter__()

    @tp.override
    async def commit(self) -> None:
        if self.fail:
            raise self.fail
        self.committed = True

    @tp.override
    async def rollback(self) -> None:
        """Leaving without a commit undoes this unit of work's writes, as PostgreSQL does."""
        if not self.committed:
            for live, saved in zip(self._state(), self._snapshot):
                live.clear()
                live.update(saved)


class FakeUowFactory:
    """Every unit of work shares one repository; set fail to make commits raise."""

    def __init__(self) -> None:
        self.repo = FakeMessageRepository()
        self.uows: list[FakeUnitOfWork] = []
        self.fail: Exception | None = None

    def __call__(self) -> FakeUnitOfWork:
        uow = FakeUnitOfWork(self.repo, self.fail)
        self.uows.append(uow)
        return uow


@pytest.fixture
def uow_factory() -> FakeUowFactory:
    return FakeUowFactory()
