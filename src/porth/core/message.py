"""Core message models and types."""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class MessageStatus(str, Enum):
    PENDING = 'pending'
    QUEUED = 'queued'
    SENT = 'sent'
    DELIVERED = 'delivered'
    FAILED = 'failed'
    EXPIRED = 'expired'


class MessageType(str, Enum):
    SMS = 'sms'
    DELIVERY_RECEIPT = 'delivery_receipt'


class SMSMessage(BaseModel):
    """Core SMS message model."""

    # Message identification
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: Optional[str] = None

    # Message content
    source_addr: str
    destination_addr: str
    message_text: str

    # Message properties
    message_type: MessageType = MessageType.SMS
    status: MessageStatus = MessageStatus.PENDING

    # Protocol information
    protocol: str  # "smpp", "http", "kannel"
    protocol_data: Dict[str, Any] = Field(default_factory=dict)

    # Timing
    created_at: datetime = Field(default_factory=datetime.utcnow)
    scheduled_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None

    # Retry information
    retry_count: int = 0
    max_retries: int = 3

    # DLR information
    dlr_requested: bool = True
    dlr_url: Optional[str] = None

    class Config:
        use_enum_values = True


class DeliveryReceipt(BaseModel):
    """Delivery receipt model."""

    original_message_id: str
    delivery_status: str
    delivery_time: datetime = Field(default_factory=datetime.utcnow)
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    # Protocol-specific data
    protocol_data: Dict[str, Any] = Field(default_factory=dict)
