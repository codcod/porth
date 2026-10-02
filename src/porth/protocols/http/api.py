"""HTTP/REST API implementation using aiohttp."""

import logging
import typing as tp
from datetime import datetime

from aiohttp import web, web_request, web_response

from sqlalchemy.exc import SQLAlchemyError

from porth.config.settings import Settings
from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from porth.core.routing import Router
from porth.service_layer.unit_of_work import AbstractUnitOfWork

logger = logging.getLogger(__name__)


def create_http_app(
    queues: tp.Mapping[str, MessageQueue],
    router: Router,
    uow_factory: tp.Callable[[], AbstractUnitOfWork],
    settings: Settings,
) -> web.Application:
    """Create aiohttp application with SMS API routes."""
    app = web.Application()

    # Store dependencies in app
    app['queues'] = queues
    app['router'] = router
    app['uow_factory'] = uow_factory
    app['settings'] = settings

    # Add routes
    api_v1 = web.Application()
    api_v1['main_app'] = app  # Reference to main app for message queue access

    api_v1.router.add_post('/sms/send', send_sms)
    api_v1.router.add_get('/sms/status/{message_id}', get_sms_status)

    app.add_subapp('/api/v1', api_v1)
    app.router.add_get('/health', health_check)

    return app


# Every submit field porth knows; any other is refused (design.md §4.1)
_FIELDS = frozenset(
    {
        'from_number',
        'source_addr',
        'to_number',
        'destination_addr',
        'message',
        'message_text',
        'priority',
    }
)


def text_field(data: tp.Mapping[str, tp.Any], *names: str) -> str:
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

        # A silently ignored field would leave the client relying on behaviour
        # porth does not have (a dlr_url callback, say)
        unknown = data.keys() - _FIELDS
        if unknown:
            raise ValueError(f'unknown field(s): {sorted(unknown)}')
        priority = data.get('priority', 'normal')
        if priority not in ('high', 'normal'):
            raise ValueError("priority must be 'high' or 'normal'")

        # Support both field naming conventions
        message = SMSMessage(
            source_addr=text_field(data, 'from_number', 'source_addr'),
            destination_addr=text_field(data, 'to_number', 'destination_addr'),
            message_text=text_field(data, 'message', 'message_text'),
            protocol='http',
            protocol_data={
                'client_ip': request.remote,
                'user_agent': request.headers.get('User-Agent', ''),
            },
            status=MessageStatus.QUEUED,
            priority=priority,
        )
        # NoRoute is a 400 like any other rejection
        message.smsc = request.app['main_app']['router'].route(message.destination_addr)
    except Exception as e:
        logger.error(f'Error sending SMS via HTTP API: {e}')
        return web.json_response(
            {'error': 'Failed to send SMS', 'details': str(e)}, status=400
        )

    # Durable before it is accepted (design.md §4.6), so a restart still sends it
    main_app = request.app['main_app']
    try:
        async with main_app['uow_factory']() as uow:
            await uow.messages.add(message)
            await uow.commit()
    except (SQLAlchemyError, OSError) as e:  # OSError: database unreachable
        logger.error(f'HTTP API: message not stored, so not accepted: {e!r}')
        return web.json_response(
            {'error': 'Failed to send SMS', 'details': 'message store unavailable'},
            status=503,
        )
    await main_app['queues'][message.smsc].put(message)

    logger.info(f'HTTP API: Queued message {message.message_id}')
    return web.json_response(
        {
            'message_id': message.message_id,
            'status': 'queued',
            'message': 'Message queued for delivery',
        },
        status=200,
    )


async def get_sms_status(request: web_request.Request) -> web_response.Response:
    """Get SMS message status."""
    message_id = request.match_info['message_id']
    async with request.app['main_app']['uow_factory']() as uow:
        message = await uow.messages.get(message_id)
    if message is None:
        return web.json_response(
            {'error': 'Message not found', 'details': message_id}, status=404
        )
    return web.json_response(
        {
            'message_id': message.message_id,
            'status': message.status.value,
            'created_at': _ts(message.created_at),
            'sent_at': _ts(message.sent_at),
            'delivered_at': _ts(message.delivered_at),
        },
        status=200,
    )


def _ts(dt: tp.Optional[datetime]) -> tp.Optional[str]:
    """UTC datetime as YYYY-MM-DDTHH:MM:SSZ, or None."""
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


async def health_check(request: web_request.Request) -> web_response.Response:
    """Health check endpoint."""

    queues = request.app['queues']

    health_data = {
        'status': 'healthy',
        'queue_size': sum(q.qsize() for q in queues.values()),
        'timestamp': '2024-01-01T00:00:00Z',  # TODO: Use actual timestamp
    }

    return web.json_response(health_data, status=200)
