"""Core exceptions."""


class PorthException(Exception):
    """Base exception for Porth SMS Gateway."""

    pass


class ConfigurationError(PorthException):
    """Configuration-related errors."""

    pass


class MessageError(PorthException):
    """Message processing errors."""

    pass


class ProtocolError(PorthException):
    """Protocol-specific errors."""

    pass


class DeliveryError(PorthException):
    """Message delivery errors."""

    pass


class QueueError(PorthException):
    """Message queue errors."""

    pass
