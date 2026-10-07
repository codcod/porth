"""Message repository: SMSMessage rows, their SMSC ids and pending final-status calls."""

import abc
import typing as tp
from datetime import datetime

import sqlalchemy as sa
from monobase.repository import AbstractRepository
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from porth.adapters.tables import dlr_callbacks, messages, smsc_ids
from porth.core.message import MessageStatus, SMSMessage

_UNSENT = (MessageStatus.PENDING.value, MessageStatus.QUEUED.value)
_FINISHED = (
    MessageStatus.DELIVERED.value,
    MessageStatus.FAILED.value,
    MessageStatus.EXPIRED.value,
)


class AbstractMessageRepository(AbstractRepository[SMSMessage]):
    @abc.abstractmethod
    async def get_by_idempotency_key(self, key: str) -> SMSMessage | None: ...

    @abc.abstractmethod
    async def update(self, message: SMSMessage) -> None:
        """Write status, retry_count, sent_at, delivered_at, protocol_data and smsc;
        blank the text of a keep_text=False message once it is sent or final."""

    @abc.abstractmethod
    async def add_smsc_ids(self, message_id: str, smsc: str, ids: list[str]) -> None:
        """Index smsc's ids to message_id; an id already indexed moves to this message."""

    @abc.abstractmethod
    async def get_by_smsc_id_for_update(
        self, smsc: str, smsc_id: str
    ) -> SMSMessage | None:
        """The message smsc's id belongs to, row-locked until the commit."""

    @abc.abstractmethod
    async def unsent(self) -> list[SMSMessage]:
        """pending/queued messages, oldest first."""

    @abc.abstractmethod
    async def unreceipted(self, cutoff: datetime, limit: int) -> list[SMSMessage]:
        """Up to limit sent messages awaiting a receipt since before cutoff, oldest
        first, row-locked until the commit; rows another transaction holds are skipped."""

    @abc.abstractmethod
    async def evict(self, cutoff: datetime, limit: int) -> int:
        """Delete up to limit finished messages created before cutoff (with their SMSC
        ids); the number deleted. A message awaiting a receipt is not finished, and one
        whose final-status call is still stored is kept until the call is made."""

    @abc.abstractmethod
    async def add_callback(
        self, message_id: str, url: str, body: dict[str, tp.Any] | None = None
    ) -> None:
        """Store a call: a GET of url when body is None, else a POST of body."""

    @abc.abstractmethod
    async def delete_callback(self, message_id: str) -> None: ...

    @abc.abstractmethod
    async def callbacks(self) -> list[tuple[str, str, dict[str, tp.Any] | None]]:
        """(message_id, url, body) of every call not yet made."""


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
    async def get_by_idempotency_key(self, key: str) -> SMSMessage | None:
        result = await self.connection.execute(
            sa.select(messages).where(messages.c.idempotency_key == key)
        )
        row = result.one_or_none()
        return _message(row) if row else None

    @tp.override
    async def update(self, message: SMSMessage) -> None:
        values: dict[str, tp.Any] = dict(
            status=message.status.value,
            retry_count=message.retry_count,
            sent_at=message.sent_at,
            delivered_at=message.delivered_at,
            protocol_data=message.protocol_data,
            smsc=message.smsc,
        )
        # Not kept once nothing will send it again (design.md §4.1); NOT NULL, so ''
        if not message.keep_text and message.status.value not in _UNSENT:
            values['message_text'] = ''
        await self.connection.execute(
            sa.update(messages)
            .where(messages.c.message_id == message.message_id)
            .values(values)
        )

    @tp.override
    async def add_smsc_ids(self, message_id: str, smsc: str, ids: list[str]) -> None:
        if not ids:
            return
        stmt = insert(smsc_ids).values(
            [{'smsc': smsc, 'smsc_id': i, 'message_id': message_id} for i in ids]
        )
        await self.connection.execute(
            stmt.on_conflict_do_update(
                index_elements=[smsc_ids.c.smsc, smsc_ids.c.smsc_id],
                set_={'message_id': stmt.excluded.message_id},
            )
        )

    @tp.override
    async def get_by_smsc_id_for_update(
        self, smsc: str, smsc_id: str
    ) -> SMSMessage | None:
        result = await self.connection.execute(
            sa.select(messages)
            .join(smsc_ids, smsc_ids.c.message_id == messages.c.message_id)
            .where(smsc_ids.c.smsc == smsc, smsc_ids.c.smsc_id == smsc_id)
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
    async def unreceipted(self, cutoff: datetime, limit: int) -> list[SMSMessage]:
        result = await self.connection.execute(
            sa.select(messages)
            .where(
                messages.c.status == MessageStatus.SENT.value,
                messages.c.dlr_requested,
                messages.c.sent_at < cutoff,
            )
            .order_by(messages.c.sent_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_message(row) for row in result]

    @tp.override
    async def evict(self, cutoff: datetime, limit: int) -> int:
        finished = sa.or_(
            messages.c.status.in_(_FINISHED),
            sa.and_(
                messages.c.status == MessageStatus.SENT.value,
                sa.not_(messages.c.dlr_requested),
            ),
        )
        doomed = (
            sa.select(messages.c.message_id)
            .where(
                messages.c.created_at < cutoff,
                finished,
                # Its call is still owed: the cascade would lose it on a restart
                ~sa.exists().where(dlr_callbacks.c.message_id == messages.c.message_id),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            # Evaluated once: as a plain IN subquery PostgreSQL may re-run it, and
            # delete more than limit
            .cte('doomed')
            .prefix_with('MATERIALIZED')
        )
        result = await self.connection.execute(
            sa.delete(messages).where(
                messages.c.message_id.in_(sa.select(doomed.c.message_id))
            )
        )
        return result.rowcount

    @tp.override
    async def add_callback(
        self, message_id: str, url: str, body: dict[str, tp.Any] | None = None
    ) -> None:
        await self.connection.execute(
            sa.insert(dlr_callbacks).values(message_id=message_id, url=url, body=body)
        )

    @tp.override
    async def delete_callback(self, message_id: str) -> None:
        await self.connection.execute(
            sa.delete(dlr_callbacks).where(dlr_callbacks.c.message_id == message_id)
        )

    @tp.override
    async def callbacks(self) -> list[tuple[str, str, dict[str, tp.Any] | None]]:
        result = await self.connection.execute(
            sa.select(
                dlr_callbacks.c.message_id, dlr_callbacks.c.url, dlr_callbacks.c.body
            )
        )
        return [(row.message_id, row.url, row.body) for row in result]


def _message(row: sa.Row) -> SMSMessage:
    fields = row._asdict()
    fields['status'] = MessageStatus(fields['status'])
    return SMSMessage(**fields)
