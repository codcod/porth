"""Kannel-compatible API implementation."""

import logging
import typing as tp

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from porth.core.exceptions import NoRoute
from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from porth.core.routing import Router
from porth.service_layer.unit_of_work import AbstractUnitOfWork
from porth.protocols.http.api import text_field

logger = logging.getLogger(__name__)

# Kannel's default sendsms-chars, less the space that separates recipients
_TO_CHARS = frozenset('0123456789+-')


def create_kannel_app(
    queues: tp.Mapping[str, MessageQueue],
    router: Router,
    uow_factory: tp.Callable[[], AbstractUnitOfWork],
    default_sender: tp.Optional[str] = None,
) -> web.Application:
    """Create the aiohttp application serving Kannel's GET /cgi-bin/sendsms."""
    app = web.Application()
    app['queues'] = queues
    app['router'] = router
    app['uow_factory'] = uow_factory
    app['default_sender'] = default_sender
    app.router.add_get('/cgi-bin/sendsms', kannel_send_sms, allow_head=False)
    return app


async def kannel_send_sms(request: web.Request) -> web.Response:
    """Kannel-compatible SMS send endpoint."""
    try:
        # Parse query parameters (Kannel style)
        params = request.query

        # Kannel sends once to every distinct space-separated number, silently
        # dropping any with a character outside sendsms-chars
        recipients = []
        for to in params.get('to', '').split():
            if _TO_CHARS.issuperset(to):
                recipients.append(to)
            else:
                logger.info(f'Kannel API: dropping recipient {to!r}')
        recipients = list(dict.fromkeys(recipients))
        if not recipients:
            raise ValueError('no valid recipient in to')

        source_addr = params.get('from') or request.app['default_sender']
        if not source_addr:
            raise ValueError('from is required')
        message_text = text_field(params, 'text')

        # No callback without an integer mask; rejecting unsupported bits is POR-005's
        try:
            dlr_mask = int(params.get('dlr-mask', ''))
        except ValueError:
            dlr_mask = 0

        # Each recipient routed once (smsc: an override, not Kannel's hint); one
        # nothing routes is dropped, as Kannel does
        smsc = params.get('smsc') or None
        routes = {}
        for to in recipients:
            try:
                routes[to] = request.app['router'].route(to, smsc)
            except NoRoute as e:
                logger.info(f'Kannel API: dropping recipient {to!r}: {e}')

        # One internal message per recipient; username/password are never read (design.md §2)
        messages = [
            SMSMessage(
                source_addr=source_addr,
                destination_addr=to,
                message_text=message_text,
                protocol='kannel',
                protocol_data={'dlr_mask': dlr_mask},
                dlr_url=params.get('dlr-url'),
                status=MessageStatus.QUEUED,
                smsc=route,
            )
            for to, route in routes.items()
        ]
    except Exception as e:
        logger.error(f'Error in Kannel SMS send: {e}')
        return web.Response(
            text=f'3: Failed to send SMS: {str(e)}',
            content_type='text/plain',
            status=400,
        )
    if not messages:
        # Kannel's answer, word for word
        return web.Response(
            text='Not routable. Do not try again.',
            content_type='text/plain',
            status=403,
        )

    # Durable before accepted (design.md §4.6), and all or none of the request's messages
    try:
        async with request.app['uow_factory']() as uow:
            for message in messages:
                await uow.messages.add(message)
            await uow.commit()
    except (SQLAlchemyError, OSError) as e:  # OSError: database unreachable
        logger.error(f'Kannel API: message not stored, so not accepted: {e!r}')
        return web.Response(
            text='3: Failed to send SMS: message store unavailable',
            content_type='text/plain',
            status=503,
        )
    for message in messages:
        await request.app['queues'][message.smsc].put(message)
        logger.info(f'Kannel API: Queued message {message.message_id}')

    ids = ''.join(f'\nMessage-ID: {m.message_id}' for m in messages)
    return web.Response(
        text=f'0: Accepted for delivery{ids}', content_type='text/plain'
    )
