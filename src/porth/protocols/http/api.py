"""HTTP/REST API implementation using aiohttp."""

import logging
from aiohttp import web, web_request, web_response
from porth.core.message import SMSMessage
from porth.core.queue import MessageQueue
from porth.config.settings import Settings
from porth.protocols.http.models import SMSRequest, SMSResponse, StatusResponse

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


async def send_sms(request: web_request.Request) -> web_response.Response:
    """Send SMS message via HTTP API."""
    try:
        # Parse request body
        data = await request.json()

        # Support both field naming conventions
        normalized_data = {
            'from_number': data.get('from_number') or data.get('source_addr'),
            'to_number': data.get('to_number') or data.get('destination_addr'),
            'message': data.get('message') or data.get('message_text'),
            'dlr_url': data.get('dlr_url'),
        }

        sms_request = SMSRequest(**normalized_data)

        # Create internal message
        message = SMSMessage(
            source_addr=sms_request.from_number,
            destination_addr=sms_request.to_number,
            message_text=sms_request.message,
            protocol='http',
            protocol_data={
                'client_ip': request.remote,
                'user_agent': request.headers.get('User-Agent', ''),
                'dlr_url': sms_request.dlr_url,
            },
            dlr_requested=sms_request.dlr_url is not None,
            dlr_url=sms_request.dlr_url,
        )

        # Add to message queue
        # message_queue = request.app['message_queue']
        main_app = request.app['main_app']
        message_queue = main_app['message_queue']
        await message_queue.put(message)

        # Return response
        response = SMSResponse(
            message_id=message.message_id,
            status='queued',
            message='Message queued for delivery',
        )

        logger.info(f'HTTP API: Queued message {message.message_id}')
        return web.json_response(response.dict(), status=200)

    except Exception as e:
        logger.error(f'Error sending SMS via HTTP API: {e}')
        return web.json_response(
            {'error': 'Failed to send SMS', 'details': str(e)}, status=400
        )


async def get_sms_status(request: web_request.Request) -> web_response.Response:
    """Get SMS message status."""
    try:
        message_id = request.match_info['message_id']

        # TODO: Implement message status lookup
        # For now, return a placeholder response
        response = StatusResponse(
            message_id=message_id,
            status='pending',
            created_at='2024-01-01T00:00:00Z',
            sent_at=None,
            delivered_at=None,
        )

        return web.json_response(response.model_dump(), status=200)

    except Exception as e:
        logger.error(f'Error getting SMS status: {e}')
        return web.json_response(
            {'error': 'Failed to get status', 'details': str(e)}, status=400
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
