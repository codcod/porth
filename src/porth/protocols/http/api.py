"""HTTP/REST API implementation using aiohttp."""

import logging
import typing as tp

from aiohttp import web, web_request, web_response

from porth.config.settings import Settings
from porth.core.message import SMSMessage
from porth.core.queue import MessageQueue

logger = logging.getLogger(__name__)


def create_http_app(message_queue: MessageQueue, settings: Settings) -> web.Application:
    """Create aiohttp application with SMS API routes."""
    app = web.Application()

    # Store dependencies in app
    app['message_queue'] = message_queue
    app['settings'] = settings

    # Add routes
    api_v1 = web.Application()
    api_v1['main_app'] = app  # Reference to main app for message queue access

    api_v1.router.add_post('/sms/send', send_sms)
    api_v1.router.add_get('/sms/status/{message_id}', get_sms_status)

    app.add_subapp('/api/v1', api_v1)
    app.router.add_get('/health', health_check)

    return app


def text_field(data: dict[str, tp.Any], *names: str) -> str:
    """The first non-empty value among names (aliases), which must be a string."""
    for name in names:
        value = data.get(name)
        if value:
            if not isinstance(value, str):
                raise ValueError(f'{name} must be a string')
            return value
    raise ValueError(f'{names[0]} is required')


async def send_sms(request: web_request.Request) -> web_response.Response:
    """Send SMS message via HTTP API."""
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('request body must be a JSON object')

        dlr_url = data.get('dlr_url') or None
        if dlr_url is not None and not isinstance(dlr_url, str):
            raise ValueError('dlr_url must be a string')

        # Support both field naming conventions
        message = SMSMessage(
            source_addr=text_field(data, 'from_number', 'source_addr'),
            destination_addr=text_field(data, 'to_number', 'destination_addr'),
            message_text=text_field(data, 'message', 'message_text'),
            protocol='http',
            protocol_data={
                'client_ip': request.remote,
                'user_agent': request.headers.get('User-Agent', ''),
                'dlr_url': dlr_url,
            },
            dlr_requested=dlr_url is not None,
            dlr_url=dlr_url,
        )

        # Add to message queue
        main_app = request.app['main_app']
        message_queue = main_app['message_queue']
        await message_queue.put(message)

        logger.info(f'HTTP API: Queued message {message.message_id}')
        return web.json_response(
            {
                'message_id': message.message_id,
                'status': 'queued',
                'message': 'Message queued for delivery',
            },
            status=200,
        )

    except Exception as e:
        logger.error(f'Error sending SMS via HTTP API: {e}')
        return web.json_response(
            {'error': 'Failed to send SMS', 'details': str(e)}, status=400
        )


async def get_sms_status(request: web_request.Request) -> web_response.Response:
    """Get SMS message status."""
    message_id = request.match_info['message_id']

    # TODO: Implement message status lookup (POR-009)
    # For now, return a placeholder response
    return web.json_response(
        {
            'message_id': message_id,
            'status': 'pending',
            'created_at': '2024-01-01T00:00:00Z',
            'sent_at': None,
            'delivered_at': None,
        },
        status=200,
    )


async def health_check(request: web_request.Request) -> web_response.Response:
    """Health check endpoint."""

    message_queue = request.app['message_queue']

    health_data = {
        'status': 'healthy',
        'queue_size': message_queue.qsize(),
        'timestamp': '2024-01-01T00:00:00Z',  # TODO: Use actual timestamp
    }

    return web.json_response(health_data, status=200)
