"""Kannel-compatible API implementation."""

import logging
import typing as tp

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from porth.service_layer.unit_of_work import AbstractUnitOfWork
from porth.protocols.http.api import text_field

logger = logging.getLogger(__name__)


def create_kannel_app(
    message_queue: MessageQueue, uow_factory: tp.Callable[[], AbstractUnitOfWork]
) -> web.Application:
    """Create the aiohttp application serving Kannel's GET /cgi-bin/sendsms."""
    app = web.Application()
    app['message_queue'] = message_queue
    app['uow_factory'] = uow_factory
    app.router.add_get('/cgi-bin/sendsms', kannel_send_sms, allow_head=False)
    return app


async def kannel_send_sms(request: web.Request) -> web.Response:
    """Kannel-compatible SMS send endpoint."""
    try:
        # Parse query parameters (Kannel style)
        params = request.query

        # Kannel separates several recipients with spaces; porth sends to one
        recipients = text_field(params, 'to').split()
        if len(recipients) != 1:
            raise ValueError('to must be a single recipient')

        # No callback without an integer mask; rejecting unsupported bits is POR-005's
        try:
            dlr_mask = int(params.get('dlr-mask', ''))
        except ValueError:
            dlr_mask = 0

        # Create internal message; username/password are never read (design.md §2)
        message = SMSMessage(
            source_addr=text_field(params, 'from'),
            destination_addr=recipients[0],
            message_text=text_field(params, 'text'),
            protocol='kannel',
            protocol_data={'dlr_mask': dlr_mask},
            dlr_url=params.get('dlr-url'),
            status=MessageStatus.QUEUED,
        )
    except Exception as e:
        logger.error(f'Error in Kannel SMS send: {e}')
        return web.Response(
            text=f'3: Failed to send SMS: {str(e)}',
            content_type='text/plain',
            status=400,
        )

    # Durable before it is accepted (design.md §4.6), so a restart still sends it
    try:
        async with request.app['uow_factory']() as uow:
            await uow.messages.add(message)
            await uow.commit()
    except (SQLAlchemyError, OSError) as e:  # OSError: database unreachable
        logger.error(f'Kannel API: message not stored, so not accepted: {e!r}')
        return web.Response(
            text='3: Failed to send SMS: message store unavailable',
            content_type='text/plain',
            status=503,
        )
    await request.app['message_queue'].put(message)

    logger.info(f'Kannel API: Queued message {message.message_id}')
    return web.Response(
        text=f'0: Accepted for delivery\nMessage-ID: {message.message_id}',
        content_type='text/plain',
    )
