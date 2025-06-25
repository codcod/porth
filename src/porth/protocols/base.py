"""Base class for protocol handlers."""

from abc import ABC, abstractmethod
from typing import Any, Dict
from porth.core.message import SMSMessage


class ProtocolHandler(ABC):
    """Base class for protocol handlers."""

    @abstractmethod
    async def start(self) -> None:
        """Start the protocol handler."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the protocol handler."""
        pass

    @abstractmethod
    async def send_message(self, message: SMSMessage) -> Dict[str, Any]:
        """Send a message via this protocol."""
        pass

    @abstractmethod
    async def handle_delivery_receipt(self, receipt_data: Dict[str, Any]) -> None:
        """Handle a delivery receipt."""
        pass
