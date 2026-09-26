"""Kannel-compatible API implementation."""

import logging

from aiohttp import web

from porth.core.message import SMSMessage

logger = logging.getLogger(__name__)


async def kannel_send_sms(request: web.Request) -> web.Response:
    """Kannel-compatible SMS send endpoint."""
    try:
        # Parse query parameters (Kannel style)
        params = request.query

        # Create internal message; username/password are never checked (design.md §2)
        message = SMSMessage(
            source_addr=params.get('from', ''),
            destination_addr=params.get('to', ''),
            message_text=params.get('text', ''),
            protocol='kannel',
            protocol_data={
                'username': params.get('username', ''),
                'dlr_mask': params.get('dlr-mask', '1'),
            },
            dlr_url=params.get('dlr-url'),
        )

        # Add to message queue
        message_queue = request.app['message_queue']
        await message_queue.put(message)

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


async def kannel_status(request: web.Request) -> web.Response:
    """Kannel-compatible status endpoint."""
    message_queue = request.app['message_queue']

    status_text = f"""Kannel bearerbox status
Queue size: {message_queue.qsize()}
Status: running
"""

    return web.Response(text=status_text, content_type='text/plain')
