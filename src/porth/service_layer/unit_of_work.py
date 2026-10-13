"""porth's unit of work: one transaction over the message repository."""

import typing as tp

from monobase import uow

from porth.adapters.repository import (
    AbstractMessageRepository,
    SqlAlchemyMessageRepository,
)


class AbstractUnitOfWork(uow.AbstractUnitOfWork):
    messages: AbstractMessageRepository


class SqlAlchemyUnitOfWork(AbstractUnitOfWork, uow.SqlAlchemyUnitOfWork):
    @tp.override
    async def __aenter__(self) -> tp.Self:
        try:
            await super().__aenter__()
        except BaseException:
            # __aexit__ never runs for a failed __aenter__, so the connection would
            # stay checked out: SQLite's BEGIN IMMEDIATE fails once busy_timeout passes
            if (connection := getattr(self, 'connection', None)) is not None:
                await connection.close()
            raise
        self.messages = SqlAlchemyMessageRepository(self.connection)
        return self
