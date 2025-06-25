"""Kannel-compatible API implementation."""

import logging
from aiohttp import web
from porth.core.message import SMSMessage
from porth.protocols.kannel.models import KannelSMSRequest

logger = logging.getLogger(__name__)


async def kannel_send_sms(request: web.Request) -> web.Response:
    """Kannel-compatible SMS send endpoint."""
    try:
        # Parse query parameters (Kannel style)
        params = dict(request.query)

        # Convert to internal format
        kannel_request = KannelSMSRequest(
            username=params.get('username', ''),
            password=params.get('password', ''),
            to=params.get('to', ''),
            **{'from': params.get('from', '')},
            text=params.get('text', ''),
            **{'dlr-url': params.get('dlr-url')},
            **{'dlr-mask': params.get('dlr-mask', '1')},
        )

        # Create internal message
        message = SMSMessage(
            source_addr=kannel_request.from_,
            destination_addr=kannel_request.to,
            message_text=kannel_request.text,
            protocol='kannel',
            protocol_data={
                'username': kannel_request.username,
                'dlr_mask': kannel_request.dlr_mask,
            },
            dlr_url=kannel_request.dlr_url,
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
