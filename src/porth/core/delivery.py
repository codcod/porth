"""Delivery engine with retry logic."""

import asyncio
import logging
import typing as tp
from datetime import datetime, timezone

from smpp import CommandStatus
from smpp.exceptions import SMPPMessageException

from porth.config.settings import DeliveryConfig, Settings
from porth.core.exceptions import DeliveryError, MessageError
from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from porth.service_layer.unit_of_work import AbstractUnitOfWork

if tp.TYPE_CHECKING:
    from porth.protocols.smpp.client import SMPPClient

logger = logging.getLogger(__name__)

# Kannel's split: these submit_sm statuses are retried, any other fails the message at once
_TRANSIENT = {
    CommandStatus.ESME_RTHROTTLED,
    CommandStatus.ESME_RMSGQFUL,
    CommandStatus.ESME_RX_T_APPN,
    CommandStatus.ESME_RSYSERR,
    # not Kannel's: the SMSC lost our session, a verdict on the bind, not the message
    CommandStatus.ESME_RINVBNDSTS,
}

# Waits before retrying a failed `sent` write. They sum to under dlr's re-check window
# (15 s), so a receipt that beat the write still finds its ids once a retry lands.
_SENT_WRITE_WAITS = (1, 2, 4)


def retry_delay(attempt: int, config: DeliveryConfig) -> int:
    """Seconds to wait before retry `attempt` (1-based)."""
    return min(
        int(config.retry_delay * config.backoff_factor ** (attempt - 1)),
        config.max_retry_delay,
    )


class DeliveryEngine:
    """Async delivery engine with retry logic."""

    def __init__(
        self,
        message_queue: MessageQueue,
        uow_factory: tp.Callable[[], AbstractUnitOfWork],
        settings: Settings,
    ):
        self.message_queue = message_queue
        self.uow_factory = uow_factory
        self.settings = settings
        self.workers: list[asyncio.Task] = []
        self.running = False
        self._retries: set[asyncio.Task] = set()
        self.smpp_client: tp.Optional['SMPPClient'] = None

    async def start(self) -> None:
        """Start the delivery engine workers."""
        if self.running:
            return

        self.running = True

        # Start delivery workers
        for i in range(self.settings.delivery.worker_count):
            worker = asyncio.create_task(self._delivery_worker(f'worker-{i}'))
            self.workers.append(worker)

        logger.info(
            f'Delivery engine started with {self.settings.delivery.worker_count} workers'
        )

    async def stop(self) -> None:
        """Stop the delivery engine workers."""
        if not self.running:
            return

        self.running = False

        # Cancel all workers and pending retries (a message waiting to retry stays
        # queued in the store, and the next start queues it again)
        tasks = [*self.workers, *self._retries]
        for task in tasks:
            task.cancel()

        # Wait for them to finish
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()

        logger.info('Delivery engine stopped')

    async def _delivery_worker(self, worker_name: str) -> None:
        """Delivery worker that processes messages from the queue."""
        logger.info(f'Delivery worker {worker_name} started')

        while self.running:
            try:
                # Get message from queue with timeout
                message = await self.message_queue.get(timeout=1.0)

                # Process the message
                await self._process_message(message)

                # Mark task as done
                self.message_queue.task_done()

            except asyncio.TimeoutError:
                # No message available, continue
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f'Error in delivery worker {worker_name}: {e}')

        logger.info(f'Delivery worker {worker_name} stopped')

    async def _requeue_later(self, message: SMSMessage, delay: int) -> None:
        """Put message back on the queue after delay seconds."""
        await asyncio.sleep(delay)
        await self.message_queue.put(message)

    async def _process_message(self, message: SMSMessage) -> None:
        """Process a single message."""
        try:
            logger.info(f'Processing message {message.message_id}')

            if self.smpp_client is None:
                raise DeliveryError('no SMPP client configured')

            result = await self.smpp_client.send_message(message)

        except MessageError as e:
            logger.error(f'Message {message.message_id} rejected permanently: {e}')
            message.status = MessageStatus.FAILED
            await self._save(message)
            return

        except Exception as e:
            if (
                isinstance(e, SMPPMessageException)
                and e.command_status is not None
                and e.command_status not in _TRANSIENT
            ):
                logger.error(
                    f'Message {message.message_id} rejected by the SMSC '
                    f'({_status_name(e.command_status)}), not retried'
                )
                message.status = MessageStatus.FAILED
                await self._save(message)
                return
            logger.error(f'Failed to deliver message {message.message_id}: {e}')
            await self._handle_delivery_failure(message, str(e))
            return

        message.status = MessageStatus.SENT
        message.sent_at = datetime.now(timezone.utc)
        ids = result['smsc_message_ids']
        message.protocol_data['smsc_message_ids'] = ids
        # Outside the send's try: the SMSC has the message, so a failed write must
        # never resend it now. Retried, since left `queued` a restart would resend it.
        for wait in (*_SENT_WRITE_WAITS, None):
            try:
                async with self.uow_factory() as uow:
                    await uow.messages.update(message)
                    await uow.messages.add_smsc_ids(message.message_id, ids)
                    await uow.commit()
                break
            except Exception:
                if wait is None:
                    logger.exception(
                        f'Message {message.message_id} sent as {ids}, but not saved '
                        'as sent: its receipts will not match, and a restart resends it'
                    )
                    break
                logger.warning(
                    f'Message {message.message_id} sent as {ids}, not saved yet; '
                    f'retrying in {wait}s'
                )
                await asyncio.sleep(wait)
        logger.info(f'Message {message.message_id} sent')

    async def _save(self, message: SMSMessage) -> None:
        """Write the message's status and retry count; log a failed write."""
        try:
            async with self.uow_factory() as uow:
                await uow.messages.update(message)
                await uow.commit()
        except Exception:
            logger.exception(
                f'Message {message.message_id}: {message.status.value} '
                f'(attempt {message.retry_count}) not saved'
            )

    async def _handle_delivery_failure(self, message: SMSMessage, error: str) -> None:
        """Handle delivery failure and retry logic."""
        message.retry_count += 1
        config = self.settings.delivery
        if message.retry_count >= config.max_retries:
            message.status = MessageStatus.FAILED
        await self._save(message)

        if message.retry_count < config.max_retries:
            delay = retry_delay(message.retry_count, config)
            logger.info(
                f'Message {message.message_id}: attempt {message.retry_count}/{config.max_retries} failed, retrying in {delay}s'
            )
            task = asyncio.create_task(self._requeue_later(message, delay))
            self._retries.add(task)
            task.add_done_callback(self._retries.discard)
        else:
            logger.error(
                f'Message {message.message_id}: attempt {message.retry_count}/{config.max_retries} failed, giving up'
            )


def _status_name(status: int) -> str:
    try:
        return CommandStatus(status).name
    except ValueError:  # a status smppai's enum lacks
        return f'0x{status:08x}'
