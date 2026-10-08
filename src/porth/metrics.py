"""Prometheus metrics, on prometheus_client's default registry (design.md §4.1).

Label values are configured SMSC names or closed sets, with two exceptions: a final status
written at startup for a message whose SMSC is no longer configured carries that old name
(or ''), and an SMPP error status unknown to smppai is its hex code.
"""

from prometheus_client import Counter, Gauge, Histogram

submitted = Counter(
    'porth_messages_submitted', 'Messages accepted for sending', ['protocol']
)
final = Counter(
    'porth_messages_final', 'Messages reaching a final status', ['smsc', 'status']
)
retries = Counter('porth_send_retries', 'Send retries scheduled', ['smsc'])
smpp_errors = Counter(
    'porth_smpp_errors',
    'submit_sm errors by command_status',
    ['smsc', 'command_status'],
)
receipts = Counter(
    'porth_receipts',
    'Delivery receipts, matched to a message or not',
    ['smsc', 'matched'],
)
mo = Counter('porth_mo', 'MO forwarded to the application', ['smsc', 'result'])
waiting = Gauge('porth_messages_waiting', 'Messages queued to send', ['smsc'])
retrying = Gauge('porth_messages_retrying', 'Messages waiting out a retry', ['smsc'])
bound = Gauge('porth_smsc_bound', '1 while the SMSC is bound', ['smsc'])
submit_seconds = Histogram(
    'porth_submit_seconds', 'submit_multipart latency, after pacing', ['smsc']
)
