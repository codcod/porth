"""Message repository: SMSMessage rows, their SMSC ids and pending dlr-url calls."""

import abc
import typing as tp

import sqlalchemy as sa
from monobase.repository import AbstractRepository
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from porth.adapters.tables import dlr_callbacks, messages, smsc_ids
from porth.core.message import MessageStatus, SMSMessage

_UNSENT = (MessageStatus.PENDING.value, MessageStatus.QUEUED.value)


class AbstractMessageRepository(AbstractRepository[SMSMessage]):
    @abc.abstractmethod
    async def update(self, message: SMSMessage) -> None:
        """Write status, retry_count, sent_at, delivered_at and protocol_data."""

    @abc.abstractmethod
    async def add_smsc_ids(self, message_id: str, ids: list[str]) -> None:
        """Index ids to message_id; an id already indexed moves to this message."""

    @abc.abstractmethod
    async def get_by_smsc_id_for_update(self, smsc_id: str) -> SMSMessage | None:
        """The message an SMSC id belongs to, row-locked until the commit."""

    @abc.abstractmethod
    async def unsent(self) -> list[SMSMessage]:
        """pending/queued messages, oldest first."""

    @abc.abstractmethod
    async def add_callback(self, message_id: str, url: str) -> None: ...

    @abc.abstractmethod
    async def delete_callback(self, message_id: str) -> None: ...

    @abc.abstractmethod
    async def callbacks(self) -> list[tuple[str, str]]:
        """(message_id, url) of every dlr-url call not yet made."""


class SqlAlchemyMessageRepository(AbstractMessageRepository):
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

    @tp.override
    async def add(self, item: SMSMessage) -> None:
        row = {c.name: getattr(item, c.name) for c in messages.columns}
        row['status'] = item.status.value
        await self.connection.execute(sa.insert(messages).values(row))

    @tp.override
    async def get(self, id: str) -> SMSMessage | None:
        result = await self.connection.execute(
            sa.select(messages).where(messages.c.message_id == id)
        )
        row = result.one_or_none()
        return _message(row) if row else None

    @tp.override
    async def update(self, message: SMSMessage) -> None:
        await self.connection.execute(
            sa.update(messages)
            .where(messages.c.message_id == message.message_id)
            .values(
                status=message.status.value,
                retry_count=message.retry_count,
                sent_at=message.sent_at,
                delivered_at=message.delivered_at,
                protocol_data=message.protocol_data,
            )
        )

    @tp.override
    async def add_smsc_ids(self, message_id: str, ids: list[str]) -> None:
        if not ids:
            return
        stmt = insert(smsc_ids).values(
            [{'smsc_id': i, 'message_id': message_id} for i in ids]
        )
        await self.connection.execute(
            stmt.on_conflict_do_update(
                index_elements=[smsc_ids.c.smsc_id],
                set_={'message_id': stmt.excluded.message_id},
            )
        )

    @tp.override
    async def get_by_smsc_id_for_update(self, smsc_id: str) -> SMSMessage | None:
        result = await self.connection.execute(
            sa.select(messages)
            .join(smsc_ids, smsc_ids.c.message_id == messages.c.message_id)
            .where(smsc_ids.c.smsc_id == smsc_id)
            .with_for_update(of=messages)
        )
        row = result.one_or_none()
        return _message(row) if row else None

    @tp.override
    async def unsent(self) -> list[SMSMessage]:
        result = await self.connection.execute(
            sa.select(messages)
            .where(messages.c.status.in_(_UNSENT))
            .order_by(messages.c.created_at)
        )
        return [_message(row) for row in result]

    @tp.override
    async def add_callback(self, message_id: str, url: str) -> None:
        await self.connection.execute(
            sa.insert(dlr_callbacks).values(message_id=message_id, url=url)
        )

    @tp.override
    async def delete_callback(self, message_id: str) -> None:
        await self.connection.execute(
            sa.delete(dlr_callbacks).where(dlr_callbacks.c.message_id == message_id)
        )

    @tp.override
    async def callbacks(self) -> list[tuple[str, str]]:
        result = await self.connection.execute(
            sa.select(dlr_callbacks.c.message_id, dlr_callbacks.c.url)
        )
        return [(row.message_id, row.url) for row in result]


def _message(row: sa.Row) -> SMSMessage:
    fields = row._asdict()
    fields['status'] = MessageStatus(fields['status'])
    return SMSMessage(**fields)
