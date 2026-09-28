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
        await super().__aenter__()
        self.messages = SqlAlchemyMessageRepository(self.connection)
        return self
