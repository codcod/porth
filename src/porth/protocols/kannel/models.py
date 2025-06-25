"""Kannel-compatible models."""

from typing import Optional
from pydantic import BaseModel, Field


class KannelSMSRequest(BaseModel):
    """Kannel SMS request model."""

    username: str = Field(..., description='Kannel username')
    password: str = Field(..., description='Kannel password')
    to: str = Field(..., description='Destination number')
    from_: str = Field(..., alias='from', description='Source number')
    text: str = Field(..., description='Message text')
    dlr_url: Optional[str] = Field(None, alias='dlr-url', description='DLR URL')
    dlr_mask: str = Field('1', alias='dlr-mask', description='DLR mask')

    class Config:
        allow_population_by_field_name = True


class KannelDLR(BaseModel):
    """Kannel delivery receipt model."""

    type: str = Field(..., description='DLR type')
    message_id: str = Field(..., description='Message ID')
    status: str = Field(..., description='Delivery status')
    timestamp: str = Field(..., description='Timestamp')
