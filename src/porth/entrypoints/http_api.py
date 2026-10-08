"""HTTP/REST API implementation using aiohttp."""

import importlib.metadata
import logging
import time
import typing as tp
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aiohttp import web, web_request, web_response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from porth import metrics
from porth.config.settings import Settings
from porth.domain.exceptions import NoRoute
from porth.domain.model import MessageStatus, SMSMessage
from porth.adapters.queue import MessageQueue
from porth.service_layer.routing import Router
from porth.service_layer import services
from porth.service_layer.unit_of_work import AbstractUnitOfWork

logger = logging.getLogger(__name__)


def create_http_app(
    queues: tp.Mapping[str, MessageQueue],
    router: Router,
    uow_factory: tp.Callable[[], AbstractUnitOfWork],
    settings: Settings,
    smsc_state: tp.Callable[[], dict[str, dict[str, tp.Any]]],
) -> web.Application:
    """Create aiohttp application with SMS API routes.

    smsc_state gives, per SMSC name, its `bound`, `waiting` and `retrying`.
    """
    app = web.Application()

    # Store dependencies in app
    app['queues'] = queues
    app['router'] = router
    app['uow_factory'] = uow_factory
    app['settings'] = settings
    app['smsc_state'] = smsc_state
    app['started'] = time.monotonic()

    # Add routes
    api_v1 = web.Application()
    api_v1['main_app'] = app  # Reference to main app for message queue access

    api_v1.router.add_post('/sms/send', send_sms)
    api_v1.router.add_get('/sms/status/{message_id}', get_sms_status)

    app.add_subapp('/api/v1', api_v1)
    app.router.add_get('/health', health_check)
    app.router.add_get('/metrics', metrics_page)
    app.router.add_get('/status', status)
    app.router.add_get('/ready', ready)

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
        'callback_url',
        'valid_until',
        'keep_text',
        'idempotency_key',
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
        # porth does not have (a Kannel-style dlr_url, say)
        unknown = data.keys() - _FIELDS
        if unknown:
            raise ValueError(f'unknown field(s): {sorted(unknown)}')
        priority = data.get('priority', 'normal')
        if priority not in ('high', 'normal'):
            raise ValueError("priority must be 'high' or 'normal'")
        # Taken as is: no escape codes (design.md §4.1)
        callback_url = data.get('callback_url')
        if callback_url is not None:
            parts = urlsplit(callback_url) if isinstance(callback_url, str) else None
            if not parts or parts.scheme not in ('http', 'https') or not parts.hostname:
                raise ValueError('callback_url must be an http(s) URL with a host')

        # RFC 3339 with an offset; one already past is taken, and expires unsent
        valid_until = data.get('valid_until')
        if valid_until is not None:
            try:
                valid_until = datetime.fromisoformat(valid_until)
            except (TypeError, ValueError):
                raise ValueError('valid_until must be an RFC 3339 time') from None
            if valid_until.tzinfo is None:
                raise ValueError('valid_until must carry a UTC offset')
            # In UTC, as stored; submit_sm's absolute time has a two-digit year
            try:
                valid_until = valid_until.astimezone(timezone.utc)
            except OverflowError:
                valid_until = None
            if valid_until is None or not 2000 <= valid_until.year < 2100:
                raise ValueError('valid_until must fall in 2000 to 2099 in UTC')
        keep_text = data.get('keep_text', True)
        if not isinstance(keep_text, bool):
            raise ValueError('keep_text must be true or false')
        idempotency_key = data.get('idempotency_key')
        if idempotency_key is not None and not (
            isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 64
        ):
            raise ValueError('idempotency_key must be a string of 1 to 64 characters')
        # PostgreSQL text holds neither: either would fail as a 503, retried forever
        if idempotency_key is not None and any(
            c == '\x00' or '\ud800' <= c <= '\udfff' for c in idempotency_key
        ):
            raise ValueError(
                'idempotency_key must not contain a NUL or a lone surrogate'
            )

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
            callback_url=callback_url,
            valid_until=valid_until,
            keep_text=keep_text,
            idempotency_key=idempotency_key,
        )
    except Exception as e:
        return _rejected(e)

    main_app = request.app['main_app']
    try:
        # Before routing, so a repeat is answered even if the routing has changed
        if idempotency_key and (first := await _by_key(main_app, idempotency_key)):
            return _repeat(first)
        message.smsc = main_app['router'].route(message.destination_addr)
        try:
            await services.submit(
                main_app['uow_factory'], main_app['queues'], [message]
            )
        except IntegrityError as e:
            # A concurrent submit with the same key committed first
            first = None
            if idempotency_key and 'messages_idempotency_key_key' in str(e.orig):
                first = await _by_key(main_app, idempotency_key)
            if first is None:
                raise
            return _repeat(first)
    except NoRoute as e:
        return _rejected(e)
    except (SQLAlchemyError, OSError) as e:  # OSError: database unreachable
        logger.error(f'HTTP API: message not stored, so not accepted: {e!r}')
        return web.json_response(
            {'error': 'Failed to send SMS', 'details': 'message store unavailable'},
            status=503,
        )
    metrics.submitted.labels('http').inc()

    logger.info(f'HTTP API: Queued message {message.message_id}')
    return web.json_response(
        {
            'message_id': message.message_id,
            'status': 'queued',
            'message': 'Message queued for delivery',
        },
        status=200,
    )


def _rejected(e: Exception) -> web_response.Response:
    logger.error(f'Error sending SMS via HTTP API: {e}')
    return web.json_response(
        {'error': 'Failed to send SMS', 'details': str(e)}, status=400
    )


async def _by_key(main_app: web.Application, key: str) -> SMSMessage | None:
    # A fresh unit of work: after a failed insert the old transaction is aborted
    uow_factory: tp.Callable[[], AbstractUnitOfWork] = main_app['uow_factory']
    async with uow_factory() as uow:
        return await uow.messages.get_by_idempotency_key(key)


def _repeat(first: SMSMessage) -> web_response.Response:
    """A repeat submit's answer: the first message, nothing new sent (design.md §4.1)."""
    return web.json_response(
        {
            'message_id': first.message_id,
            'status': first.status.value,
            'message': 'Message already accepted',
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
        'timestamp': _ts(datetime.now(timezone.utc)),
    }

    return web.json_response(health_data, status=200)


async def metrics_page(request: web_request.Request) -> web_response.Response:
    """Prometheus exposition of the default registry."""
    return web.Response(
        body=generate_latest(), headers={'Content-Type': CONTENT_TYPE_LATEST}
    )


async def status(request: web_request.Request) -> web_response.Response:
    """Version, uptime and per-SMSC state, as JSON."""
    return web.json_response(
        {
            'version': importlib.metadata.version('porth'),
            'uptime_seconds': int(time.monotonic() - request.app['started']),
            'smsc': request.app['smsc_state'](),
        }
    )


async def ready(request: web_request.Request) -> web_response.Response:
    """200 while every SMSC is bound, else 503 naming the unbound ones."""
    unbound = [n for n, s in request.app['smsc_state']().items() if not s['bound']]
    if unbound:
        return web.json_response({'ready': False, 'unbound': unbound}, status=503)
    return web.json_response({'ready': True})
