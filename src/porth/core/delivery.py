"""Delivery engine with retry logic."""

import asyncio
import logging
import typing as tp
from datetime import datetime

from porth.config.settings import Settings
from porth.core.exceptions import DeliveryError, MessageError
from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from porth.core.store import MessageStore

if tp.TYPE_CHECKING:
    from porth.protocols.smpp.client import SMPPClient

logger = logging.getLogger(__name__)


class DeliveryEngine:
    """Async delivery engine with retry logic."""

    def __init__(
        self,
        message_queue: MessageQueue,
        message_store: MessageStore,
        settings: Settings,
    ):
        self.message_queue = message_queue
        self.message_store = message_store
        self.settings = settings
        self.workers: list[asyncio.Task] = []
        self.running = False
        self.retry_queue: asyncio.Queue[SMSMessage] = asyncio.Queue()
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

        # Start retry worker
        retry_worker = asyncio.create_task(self._retry_worker())
        self.workers.append(retry_worker)

        logger.info(
            f'Delivery engine started with {self.settings.delivery.worker_count} workers'
        )

    async def stop(self) -> None:
        """Stop the delivery engine workers."""
        if not self.running:
            return

        self.running = False

        # Cancel all workers
        for worker in self.workers:
            worker.cancel()

        # Wait for workers to finish
        await asyncio.gather(*self.workers, return_exceptions=True)
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

    async def _retry_worker(self) -> None:
        """Worker that handles message retries."""
        logger.info('Retry worker started')

        while self.running:
            try:
                # Get message from retry queue
                message = await self.retry_queue.get()

                # Wait for retry delay
                await asyncio.sleep(self.settings.delivery.retry_delay)

                # Put message back in main queue for retry
                await self.message_queue.put(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f'Error in retry worker: {e}')

        logger.info('Retry worker stopped')

    async def _process_message(self, message: SMSMessage) -> None:
        """Process a single message."""
        try:
            logger.info(f'Processing message {message.message_id}')

            if self.smpp_client is None:
                raise DeliveryError('no SMPP client configured')

            result = await self.smpp_client.send_message(message)

            message.status = MessageStatus.SENT
            message.sent_at = datetime.utcnow()
            ids = [result['smsc_message_id']]
            message.protocol_data['smsc_message_ids'] = ids
            self.message_store.add_smsc_ids(message, ids)

            logger.info(f'Message {message.message_id} sent')

        except MessageError as e:
            logger.error(f'Message {message.message_id} rejected permanently: {e}')
            message.status = MessageStatus.FAILED

        except Exception as e:
            logger.error(f'Failed to deliver message {message.message_id}: {e}')
            await self._handle_delivery_failure(message, str(e))

    async def _handle_delivery_failure(self, message: SMSMessage, error: str) -> None:
        """Handle delivery failure and retry logic."""
        message.retry_count += 1

        if message.retry_count < self.settings.delivery.max_retries:
            logger.info(
                f'Scheduling retry {message.retry_count}/{self.settings.delivery.max_retries} for message {message.message_id}'
            )
            message.status = MessageStatus.QUEUED
            await self.retry_queue.put(message)
        else:
            logger.error(
                f'Message {message.message_id} failed permanently after {message.retry_count} retries'
            )
            message.status = MessageStatus.FAILED
