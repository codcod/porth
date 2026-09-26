"""Kannel-compatible API implementation."""

import logging

from aiohttp import web

from porth.core.message import SMSMessage
from porth.core.queue import MessageQueue
from porth.core.store import MessageStore
from porth.protocols.http.api import text_field

logger = logging.getLogger(__name__)


def create_kannel_app(
    message_queue: MessageQueue, message_store: MessageStore
) -> web.Application:
    """Create the aiohttp application serving Kannel's GET /cgi-bin/sendsms."""
    app = web.Application()
    app['message_queue'] = message_queue
    app['message_store'] = message_store
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

        # Create internal message; username/password are never read (design.md §2)
        message = SMSMessage(
            source_addr=text_field(params, 'from'),
            destination_addr=recipients[0],
            message_text=text_field(params, 'text'),
            protocol='kannel',
            protocol_data={'dlr_mask': params.get('dlr-mask', '1')},
            dlr_url=params.get('dlr-url'),
        )

        # Store before queueing, so a poll right after the response finds it
        request.app['message_store'].add(message)
        await request.app['message_queue'].put(message)

        # Return Kannel-style response
        response_text = f'0: Accepted for delivery\nMessage-ID: {message.message_id}'

        logger.info(f'Kannel API: Queued message {message.message_id}')
        return web.Response(text=response_text, content_type='text/plain')

    except Exception as e:
        logger.error(f'Error in Kannel SMS send: {e}')
        return web.Response(
            text=f'3: Failed to send SMS: {str(e)}',
            content_type='text/plain',
            status=400,
        )
