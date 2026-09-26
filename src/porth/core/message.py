"""Core message model."""

import dataclasses
import enum
import typing as tp
import uuid
from datetime import datetime


class MessageStatus(str, enum.Enum):
    PENDING = 'pending'
    QUEUED = 'queued'
    SENT = 'sent'
    DELIVERED = 'delivered'
    FAILED = 'failed'
    EXPIRED = 'expired'


@dataclasses.dataclass(kw_only=True)
class SMSMessage:
    """Core SMS message model (design.md §5)."""

    # Message identification
    message_id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: tp.Optional[str] = None

    # Message content
    source_addr: str
    destination_addr: str
    message_text: str

    status: MessageStatus = MessageStatus.PENDING

    # Protocol information
    protocol: str  # 'http' | 'kannel'
    protocol_data: dict[str, tp.Any] = dataclasses.field(default_factory=dict)

    # Timing
    created_at: datetime = dataclasses.field(default_factory=datetime.utcnow)
    sent_at: tp.Optional[datetime] = None
    delivered_at: tp.Optional[datetime] = None

    # Retry information
    retry_count: int = 0
    max_retries: int = 3

    # DLR information
    dlr_requested: bool = True
    dlr_url: tp.Optional[str] = None
