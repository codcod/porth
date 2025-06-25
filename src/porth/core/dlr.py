"""Delivery receipt handling and correlation."""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from porth.core.message import DeliveryReceipt, SMSMessage

logger = logging.getLogger(__name__)


class DLRHandler:
    """Delivery receipt handler with message correlation."""

    def __init__(self):
        # In-memory storage for message correlation
        # TODO: Replace with persistent storage for production
        self._message_store: Dict[str, SMSMessage] = {}

    def store_message(self, message: SMSMessage) -> None:
        """Store message for DLR correlation."""
        if message.dlr_requested:
            self._message_store[message.message_id] = message
            logger.debug(f'Stored message {message.message_id} for DLR correlation')

    def process_delivery_receipt(
        self, receipt_data: Dict[str, Any]
    ) -> Optional[DeliveryReceipt]:
        """Process incoming delivery receipt."""
        try:
            message_id = receipt_data.get('message_id')
            if not message_id:
                logger.warning('Delivery receipt missing message_id')
                return None

            original_message = self._message_store.get(message_id)
            if not original_message:
                logger.warning(
                    f'No original message found for DLR with message_id: {message_id}'
                )
                return None

            # Create delivery receipt
            receipt = DeliveryReceipt(
                original_message_id=message_id,
                delivery_status=receipt_data.get('status', 'unknown'),
                delivery_time=datetime.utcnow(),
                error_code=receipt_data.get('error_code'),
                error_message=receipt_data.get('error_message'),
                protocol_data=receipt_data,
            )

            logger.info(
                f'Processed DLR for message {message_id}: {receipt.delivery_status}'
            )

            # TODO: Send DLR back to original client via appropriate protocol

            # Clean up stored message
            del self._message_store[message_id]

            return receipt

        except Exception as e:
            logger.error(f'Error processing delivery receipt: {e}')
            return None

    def get_pending_dlr_count(self) -> int:
        """Get count of messages waiting for DLR."""
        return len(self._message_store)

    def cleanup_expired_messages(self, max_age_hours: int = 24) -> int:
        """Clean up old messages that never received DLR."""
        cutoff_time = datetime.utcnow() - timedelta(hours=max_age_hours)
        expired_messages = [
            msg_id
            for msg_id, msg in self._message_store.items()
            if msg.created_at < cutoff_time
        ]

        for msg_id in expired_messages:
            del self._message_store[msg_id]

        if expired_messages:
            logger.info(f'Cleaned up {len(expired_messages)} expired messages')

        return len(expired_messages)
