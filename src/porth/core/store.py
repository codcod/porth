"""In-process Message Store: every message's status, for the HTTP API to poll."""

import typing as tp

from porth.core.message import SMSMessage


# ponytail: grows without bound; POR-003 evicts terminal records after a retention window
class MessageStore:
    """Live SMSMessage objects by porth message id, indexed by SMSC message id.

    Status changes mutate the stored object, so there is no separate update call.
    No lock: one event loop, and no method awaits.
    """

    def __init__(self) -> None:
        self._messages: dict[str, SMSMessage] = {}
        self._by_smsc_id: dict[str, SMSMessage] = {}

    def add(self, message: SMSMessage) -> None:
        self._messages[message.message_id] = message

    def get(self, message_id: str) -> tp.Optional[SMSMessage]:
        return self._messages.get(message_id)

    def add_smsc_ids(self, message: SMSMessage, ids: list[str]) -> None:
        for smsc_id in ids:
            self._by_smsc_id[smsc_id] = message

    def find_by_smsc_id(self, smsc_id: str) -> tp.Optional[SMSMessage]:
        return self._by_smsc_id.get(smsc_id)
