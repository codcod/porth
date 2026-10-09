"""Kannel-compatible API implementation."""

import codecs
import hmac
import ipaddress
import logging
import typing as tp
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp.abc
from aiohttp import web
from smpp import DataCoding
from smpp.exceptions import SMPPPDUException
from smpp.gsm import make_parts
from sqlalchemy.exc import SQLAlchemyError

from porth import metrics
from porth.config.settings import KannelUser
from porth.adapters.smpp import choose_data_coding, sms_payload
from porth.domain.exceptions import MessageError, NoRoute
from porth.domain.model import MessageStatus, SMSMessage
from porth.adapters.queue import MessageQueue
from porth.service_layer.routing import Router
from porth.service_layer import services
from porth.service_layer.unit_of_work import AbstractUnitOfWork

logger = logging.getLogger(__name__)

# Kannel's default sendsms-chars, less the space that separates recipients
_TO_CHARS = frozenset('0123456789+-')
# POST bodies smsbox takes (its XML form is not planned)
_CONTENT_TYPES = frozenset({'text/plain', 'application/octet-stream'})
# Kannel's DCS per coding (GSM, 8-bit, UCS-2): without, with a message class
_DCS = {
    0: (DataCoding.DEFAULT, 0xF0),
    1: (DataCoding.OCTET_UNSPECIFIED_2, 0xF4),
    2: (DataCoding.UCS2, 0x18),
}
# Python codecs that are no charset (Kannel's iconv has none); punycode is quadratic
_NOT_CHARSETS = frozenset(
    {'punycode', 'idna', 'undefined', 'unicode-escape', 'raw-unicode-escape'}
)
# More bytes than 255 parts can carry in any charset (4 bytes a character at most)
_MAX_TEXT = 255 * 160 * 4


def create_kannel_app(
    queues: tp.Mapping[str, MessageQueue],
    router: Router,
    uow_factory: tp.Callable[[], AbstractUnitOfWork],
    default_sender: tp.Optional[str] = None,
    users: tp.Optional[tp.Mapping[str, KannelUser]] = None,
) -> web.Application:
    """Create the aiohttp application serving Kannel's /cgi-bin/sendsms."""
    app = web.Application()
    app['queues'] = queues
    app['router'] = router
    app['uow_factory'] = uow_factory
    app['default_sender'] = default_sender
    app['users'] = users or {}
    if not app['users']:
        logger.warning(
            'Kannel API: no porth.kannel.users configured; sendsms accepts any caller'
        )
    app.router.add_get('/cgi-bin/sendsms', kannel_send_sms, allow_head=False)
    app.router.add_post('/cgi-bin/sendsms', kannel_send_sms)
    return app


class KannelAccessLogger(aiohttp.abc.AbstractAccessLogger):
    """
    The Kannel listener's access log: the path without its query string, since the
    default format's %r would log the query-string password.
    """

    def log(
        self, request: web.BaseRequest, response: web.StreamResponse, time: float
    ) -> None:
        self.logger.info(
            f'{request.remote} "{request.method} {request.path}" '
            f'{response.status} {response.body_length}'
        )


class _Rejected(Exception):
    """A request smsbox refuses: its status and text."""

    def __init__(self, status: int, text: str):
        self.status, self.text = status, text


def _answer(status: int, text: str) -> web.Response:
    """Kannel's answer: every one is text/html."""
    return web.Response(text=text, content_type='text/html', status=status)


async def _params(
    request: web.Request,
) -> tuple[tp.Mapping[str, str], bytes, tp.Optional[bytes]]:
    """The sendsms parameters, with text and udh as raw bytes.

    GET: the query string. POST: the X-Kannel-<Name> headers as <name>, the body as
    the text and charset from Content-Type; its query string is not read.
    """
    if request.method == 'GET':
        params: tp.Mapping[str, str] = request.query
        raw: dict[str, bytes] = {}
        # latin-1 maps each percent-decoded octet to one character, and back
        for name, value in urllib.parse.parse_qsl(
            request.rel_url.raw_query_string, keep_blank_values=True, encoding='latin-1'
        ):
            if name in ('text', 'udh'):
                raw.setdefault(name, value.encode('latin-1'))
        return params, raw.get('text', b''), raw.get('udh')
    headers = {
        name.lower().removeprefix('x-kannel-'): value
        for name, value in request.headers.items()
        if name.lower().startswith('x-kannel-')
    }
    if request.charset:
        headers['charset'] = request.charset
    if request.content_type == 'application/octet-stream':
        headers.setdefault('coding', '1')
    udh = headers.get('udh')
    raw_udh = None
    if udh is not None:
        try:  # hex, else URL-encoded, as smsbox reads it
            raw_udh = bytes.fromhex(udh)
        except ValueError:
            raw_udh = urllib.parse.unquote_to_bytes(udh)
    return headers, await request.read(), raw_udh


def _int(params: tp.Mapping[str, str], *names: str) -> tp.Optional[int]:
    """The first of names (aliases) sent non-empty as an integer; None if absent or
    not an integer, which smsbox ignores."""
    for name in names:
        if value := params.get(name):
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _authorised(request: web.Request, params: tp.Mapping[str, str]) -> bool:
    """smsbox's sendsms-user check; open when no user is configured."""
    users = request.app['users']
    if not users:
        return True
    username = params.get('username', params.get('user'))
    password = params.get('password', params.get('pass'))
    user = users.get(username)
    if user is None or password is None:
        return False
    if not hmac.compare_digest(password.encode(), user.password.encode()):
        return False
    if not user.allow_ip:
        return True
    if request.remote is None:  # not a TCP peer: no address to allow
        return False
    try:
        addr = ipaddress.ip_address(request.remote)
    except ValueError:
        return False
    # ponytail: the networks are parsed per request; pre-parse them if users grow long lists
    return any(addr in ipaddress.ip_network(n) for n in user.allow_ip)


def _encoding(
    params: tp.Mapping[str, str], raw: bytes, udh: tp.Optional[bytes]
) -> tuple[str | bytes, int]:
    """The message content, text or 8-bit bytes, and its DCS (design.md §4.4); raise
    _Rejected if smsbox would."""
    coding = _int(params, 'coding')
    if coding is not None and not 0 <= coding <= 2:
        raise _Rejected(400, 'Coding field misformed, rejected')
    mclass = _int(params, 'mclass')
    if mclass is not None and not 0 <= mclass <= 3:
        raise _Rejected(400, 'MClass field misformed, rejected')
    if mclass is None and _int(params, 'flash') == 1:
        mclass = 0  # Kannel 1.4.5 ignores flash
    if udh is not None and udh[0] != len(udh) - 1:
        raise _Rejected(400, 'UDH field misformed, rejected')
    if coding is None and udh is not None:
        coding = 1  # as in Kannel

    if len(raw) > _MAX_TEXT:
        raise _Rejected(400, 'Charset or body misformed, rejected')
    content: str | bytes
    if coding == 1:
        content = raw
    else:
        charset = params.get('charset') or ('UTF-16BE' if coding == 2 else 'UTF-8')
        try:
            if codecs.lookup(charset).name in _NOT_CHARSETS:
                raise LookupError(charset)
            content = raw.decode(charset)
        # ValueError: a UnicodeError, or a name with a NUL in it
        except (LookupError, ValueError):
            raise _Rejected(400, 'Charset or body misformed, rejected') from None

    if coding is None or (coding == 0 and udh is None):
        # porth's own choice instead of Kannel's '?' for what GSM can't carry
        assert isinstance(content, str)
        try:
            data_coding, _ = choose_data_coding(content)
        except MessageError:
            raise _Rejected(400, 'Charset or body misformed, rejected') from None
        coding = 0 if data_coding == DataCoding.DEFAULT else 2
    plain, classed = _DCS[coding]
    dcs = plain if mclass is None else classed | mclass

    try:
        parts = make_parts(sms_payload(content, dcs, udh), dcs)
    except (SMPPPDUException, ValueError):  # not encodable / over 255 parts
        raise _Rejected(400, 'Charset or body misformed, rejected') from None
    if udh is not None and len(parts) > 1:
        raise _Rejected(400, 'UDH field is too long, rejected')
    return content, dcs


async def kannel_send_sms(request: web.Request) -> web.Response:
    """Kannel-compatible SMS send endpoint (GET or POST)."""
    if request.method == 'POST' and request.content_type not in _CONTENT_TYPES:
        return _answer(400, 'Invalid content-type')  # before credentials, as smsbox
    params, raw, udh = await _params(request)
    # Before any other check, as in smsbox
    if not _authorised(request, params):
        username = params.get('username', params.get('user'))
        logger.warning(
            f'Kannel API: authorization failed for {username!r} from {request.remote}'
        )
        # Kannel's answer, word for word, whichever check failed
        return _answer(403, 'Authorization failed for sendsms')
    try:
        messages = _messages(request, params, raw, udh or None)
    except _Rejected as e:
        logger.info(f'Kannel API: rejected: {e.text}')
        return _answer(e.status, e.text)
    if not messages:
        # Kannel's answer, word for word
        return _answer(403, 'Not routable. Do not try again.')

    try:
        await services.submit(
            request.app['uow_factory'], request.app['queues'], messages
        )
    except (SQLAlchemyError, OSError) as e:  # OSError: database unreachable
        logger.error(f'Kannel API: message not stored, so not accepted: {e!r}')
        return _answer(503, 'Temporal failure, try again later.')
    for message in messages:
        metrics.submitted.labels('kannel').inc()
        logger.info(f'Kannel API: Queued message {message.message_id}')

    # The Message-ID lines are porth's addition to Kannel's answer
    ids = ''.join(f'\nMessage-ID: {m.message_id}' for m in messages)
    return _answer(202, f'0: Accepted for delivery{ids}')


def _messages(
    request: web.Request,
    params: tp.Mapping[str, str],
    raw: bytes,
    udh: tp.Optional[bytes],
) -> list[SMSMessage]:
    """One message per routable recipient; raise _Rejected as smsbox would."""
    # Kannel sends once to every distinct space-separated number, silently
    # dropping any with a character outside sendsms-chars
    numbers = params.get('to', '').split()
    if not numbers:
        raise _Rejected(400, 'Missing receiver number, rejected')
    recipients = []
    for to in numbers:
        if _TO_CHARS.issuperset(to):
            recipients.append(to)
        else:
            logger.info(f'Kannel API: dropping recipient {to!r}')
    recipients = list(dict.fromkeys(recipients))
    if not recipients:
        raise _Rejected(
            400, 'Number(s) has/have been denied by white- and/or black-lists.'
        )

    source_addr = params.get('from') or request.app['default_sender']
    if not source_addr:
        raise _Rejected(400, 'Sender missing and no global set, rejected')

    content, dcs = _encoding(params, raw, udh)

    # A non-integer mask is no callback; one porth would never call is refused
    dlr_mask = _int(params, 'dlr-mask', 'dlrmask') or 0
    if dlr_mask < 0 or (dlr_mask and not dlr_mask & 0x13):
        raise _Rejected(400, 'DLR-Mask field misformed, rejected')
    dlr_url = params.get('dlr-url') or params.get('dlrurl')

    validity = _int(params, 'validity')  # minutes
    valid_until = None
    if validity is not None:
        try:
            valid_until = datetime.now(timezone.utc) + timedelta(minutes=validity)
        except OverflowError:
            pass
        # submit_sm's absolute time has a two-digit year, as the REST API's bound
        if validity < 1 or valid_until is None or valid_until.year >= 2100:
            raise _Rejected(400, 'Validity field misformed, rejected')
    # ponytail: refused, not half-honoured; future-work.md has the hold-until-due path
    if params.get('deferred'):
        raise _Rejected(400, 'Deferred field misformed, rejected')
    if params.get('priority'):
        raise _Rejected(400, 'Priority field misformed, rejected')

    protocol_data: dict[str, tp.Any] = {'dlr_mask': dlr_mask, 'data_coding': dcs}
    if udh is not None:
        protocol_data['udh'] = udh.hex()
    if isinstance(content, bytes):
        # 8-bit content, as hex: PostgreSQL text holds no NUL
        protocol_data['data'] = content.hex()

    # Each recipient routed once (smsc: an override, not Kannel's hint); one
    # nothing routes is dropped, as Kannel does
    smsc = params.get('smsc') or None
    routes = {}
    for to in recipients:
        try:
            routes[to] = request.app['router'].route(to, smsc)
        except NoRoute as e:
            logger.info(f'Kannel API: dropping recipient {to!r}: {e}')

    # One internal message per recipient; the credentials were checked above
    return [
        SMSMessage(
            source_addr=source_addr,
            destination_addr=to,
            message_text=content if isinstance(content, str) else '',
            protocol='kannel',
            protocol_data=dict(protocol_data),
            dlr_url=dlr_url,
            status=MessageStatus.QUEUED,
            smsc=route,
            valid_until=valid_until,
        )
        for to, route in routes.items()
    ]
