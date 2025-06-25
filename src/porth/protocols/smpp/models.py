"""SMPP-specific models and data structures."""

from typing import Optional
from pydantic import BaseModel


class SMPPSubmitSM(BaseModel):
    """SMPP submit_sm PDU model."""

    service_type: str = ''
    source_addr_ton: int = 0
    source_addr_npi: int = 0
    source_addr: str
    dest_addr_ton: int = 0
    dest_addr_npi: int = 0
    destination_addr: str
    esm_class: int = 0
    protocol_id: int = 0
    priority_flag: int = 0
    schedule_delivery_time: str = ''
    validity_period: str = ''
    registered_delivery: int = 1
    replace_if_present_flag: int = 0
    data_coding: int = 0
    sm_default_msg_id: int = 0
    short_message: bytes


class SMPPDeliverSM(BaseModel):
    """SMPP deliver_sm PDU model."""

    service_type: str = ''
    source_addr_ton: int = 0
    source_addr_npi: int = 0
    source_addr: str
    dest_addr_ton: int = 0
    dest_addr_npi: int = 0
    destination_addr: str
    esm_class: int = 0
    protocol_id: int = 0
    priority_flag: int = 0
    schedule_delivery_time: str = ''
    validity_period: str = ''
    registered_delivery: int = 0
    replace_if_present_flag: int = 0
    data_coding: int = 0
    sm_default_msg_id: int = 0
    short_message: bytes


class SMPPDeliveryReceipt(BaseModel):
    """SMPP delivery receipt model."""

    message_id: str
    sub: int
    dlvrd: int
    submit_date: str
    done_date: str
    stat: str
    err: str
    text: Optional[str] = None
