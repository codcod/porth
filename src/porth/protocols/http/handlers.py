"""HTTP request handlers."""

import logging
from aiohttp import web
from porth.core.message import SMSMessage
from porth.protocols.http.models import SMSRequest, SMSResponse

logger = logging.getLogger(__name__)


class SMSHandler:
    """Handler for SMS-related HTTP requests."""

    def __init__(self, message_queue, settings):
        self.message_queue = message_queue
        self.settings = settings

    async def send_sms(self, request: web.Request) -> web.Response:
        """Handle SMS send request."""
        try:
            data = await request.json()
            sms_request = SMSRequest(**data)

            message = SMSMessage(
                source_addr=sms_request.from_number,
                destination_addr=sms_request.to_number,
                message_text=sms_request.message,
                protocol='http',
                dlr_url=sms_request.dlr_url,
            )

            await self.message_queue.put(message)

            response = SMSResponse(
                message_id=message.message_id,
                status='queued',
                message='Message queued successfully',
            )

            return web.json_response(response.dict())

        except Exception as e:
            logger.error(f'Error in send_sms handler: {e}')
            return web.json_response({'error': str(e)}, status=400)
