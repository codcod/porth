"""HTTP API request/response models."""

from typing import Optional
from pydantic import BaseModel, Field


class SMSRequest(BaseModel):
    """HTTP API SMS send request model."""

    from_number: str = Field(..., description='Source phone number')
    to_number: str = Field(..., description='Destination phone number')
    message: str = Field(..., description='SMS message text')
    dlr_url: Optional[str] = Field(None, description='Delivery receipt URL')


class SMSResponse(BaseModel):
    """HTTP API SMS send response model."""

    message_id: str = Field(..., description='Unique message identifier')
    status: str = Field(..., description='Message status')
    message: str = Field(..., description='Response message')


class StatusResponse(BaseModel):
    """HTTP API status response model."""

    message_id: str = Field(..., description='Message identifier')
    status: str = Field(..., description='Current message status')
    created_at: str = Field(..., description='Message creation timestamp')
    sent_at: Optional[str] = Field(None, description='Message sent timestamp')
    delivered_at: Optional[str] = Field(None, description='Message delivered timestamp')


class ErrorResponse(BaseModel):
    """HTTP API error response model."""

    error: str = Field(..., description='Error message')
    details: Optional[str] = Field(None, description='Error details')
    code: Optional[str] = Field(None, description='Error code')
