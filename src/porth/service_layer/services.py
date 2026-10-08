"""The services the entrypoints and handlers share: accept messages, and recover the
unsent ones after a restart."""

import logging
import typing as tp
from datetime import datetime, timezone

from porth.adapters.queue import MessageQueue
from porth.domain.exceptions import NoRoute
from porth.domain.model import MessageStatus, SMSMessage
from porth.service_layer.dlr import DLRHandler
from porth.service_layer.routing import Router
from porth.service_layer.unit_of_work import AbstractUnitOfWork


async def submit(
    uow_factory: tp.Callable[[], AbstractUnitOfWork],
    queues: tp.Mapping[str, MessageQueue],
    messages: tp.Iterable[SMSMessage],
) -> None:
    """Store routed messages in one unit of work (all or none), then queue each on its
    SMSC's queue: durable before accepted (design.md §4.6), so a restart still sends
    them. Store errors propagate, and nothing is queued."""
    messages = list(messages)
    async with uow_factory() as uow:
        for message in messages:
            await uow.messages.add(message)
        await uow.commit()
    for message in messages:
        assert message.smsc is not None
        await queues[message.smsc].put(message)


async def recover(
    uow_factory: tp.Callable[[], AbstractUnitOfWork],
    router: Router,
    queues: tp.Mapping[str, MessageQueue],
    dlr_handler: DLRHandler,
) -> None:
    """Queue what a previous run accepted but did not send, oldest first (sent ones
    are not sent again). An unreachable database raises here."""
    async with uow_factory() as uow:
        unsent = await uow.messages.unsent()
    requeued = 0
    for message in unsent:
        if message.smsc not in queues:
            # Its SMSC was removed from the config, or it predates routing
            await _reroute(uow_factory, router, dlr_handler, message)
        if message.status != MessageStatus.FAILED:
            assert message.smsc is not None
            await queues[message.smsc].put(message)
            requeued += 1
    if requeued:
        logging.info(f'Re-queued {requeued} message(s) from the store')


async def _reroute(
    uow_factory: tp.Callable[[], AbstractUnitOfWork],
    router: Router,
    dlr_handler: DLRHandler,
    message: SMSMessage,
) -> None:
    """Route a recovered message again, or fail it; either way store it."""
    was = message.smsc
    try:
        message.smsc = router.route(message.destination_addr)
        logging.info(
            f'Message {message.message_id}: SMSC {was!r} is not configured, '
            f'rerouted to {message.smsc!r}'
        )
    except NoRoute as e:
        message.status = MessageStatus.FAILED
        logging.warning(
            f'Message {message.message_id}: SMSC {was!r} is not configured '
            f'and {e}: failed'
        )
    # A failed one's call is stored, not dispatched: resume() makes it, once
    # the handler has started
    async with uow_factory() as uow:
        if message.status == MessageStatus.FAILED:
            now = datetime.now(timezone.utc)
            await dlr_handler.finalize(uow, message, now)
        else:
            await uow.messages.update(message)
        await uow.commit()
