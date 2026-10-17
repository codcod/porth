"""Settings: the `[porth]` table of a TOML file (design.md §4.3)."""

import dataclasses
import ipaddress
import logging
import os
import typing as tp

import monobase.config

if tp.TYPE_CHECKING:
    from _typeshed import DataclassInstance


@dataclasses.dataclass(kw_only=True)
class HTTPUser:
    token: str  # the Bearer token, at least 16 characters
    # addresses or CIDR networks; empty: any address
    allow_ip: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(kw_only=True)
class HTTPConfig:
    host: str = '0.0.0.0'
    port: int = 8080
    # name (a label for logs) -> its REST caller; none: /api/v1 is open
    users: dict[str, HTTPUser] = dataclasses.field(default_factory=dict)
    callback_retry_minutes: int = 5  # how long a REST status callback is retried
    admin: bool = True  # False: no /admin page


@dataclasses.dataclass(kw_only=True)
class KannelUser:
    password: str
    # addresses or CIDR networks; empty: any address
    allow_ip: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(kw_only=True)
class KannelConfig:
    host: str = '0.0.0.0'
    port: int = 13013  # smsbox's sendsms default
    default_sender: tp.Optional[str] = None  # Kannel's global-sender
    # username -> its sendsms-user; none: sendsms is open
    users: dict[str, KannelUser] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(kw_only=True)
class SMPPClientConfig:
    host: str
    port: int = 2775
    system_id: str
    password: str
    system_type: str = ''
    # ponytail: int, so at least 1/s; sub-1 TPS needs a float
    throughput: tp.Optional[int] = None  # submit_sm PDUs/s; unset = unlimited
    # false: a transmitter bind sends, a receiver bind (receive_port, default port)
    # takes receipts and MO, as Kannel's transceiver-mode = false and receive-port
    transceiver: bool = True
    receive_port: tp.Optional[int] = None
    # true: both binds over TLS, the SMSC's certificate and host name always verified
    tls: bool = False
    tls_ca_file: tp.Optional[str] = None  # PEM CAs in place of the system ones
    fallback: tp.Optional[str] = None  # takes messages while this SMSC is unbound
    fallback_after: int = 10  # seconds unbound before messages move to fallback
    reconnect_delay: int = 10  # seconds between rebind attempts
    enquire_link_interval: int = 60  # seconds between keepalive enquire_links
    bind_timeout: int = 30  # seconds to wait for bind_*_resp
    # seconds to wait for any other response; also bounds the TCP and TLS connect
    response_timeout: int = 30


@dataclasses.dataclass(kw_only=True)
class RoutingConfig:
    default: tp.Optional[str] = None  # the SMSC for a number no prefix matches
    prefixes: dict[str, str] = dataclasses.field(default_factory=dict)  # digits -> SMSC


@dataclasses.dataclass(kw_only=True)
class DeliveryConfig:
    max_retries: int = 3
    retry_delay: int = 5  # seconds before the first retry
    # ponytail: ints, since the loader takes no floats; add a float branch to _coerce if
    # someone needs them
    backoff_factor: int = 2  # 1: fixed retry_delay
    max_retry_delay: int = 300  # seconds
    worker_count: int = 10  # per SMSC
    # Messages held in memory per SMSC (waiting plus retrying); at it a normal submit
    # is refused with 503, a high-priority one at twice it. Unset: unlimited
    queue_limit: tp.Optional[int] = None


@dataclasses.dataclass(kw_only=True)
class MOConfig:
    # Kannel get-url template; unset: each MO is logged and dropped
    url: tp.Optional[str] = None
    reply: bool = True  # False: Kannel's max-messages = 0


@dataclasses.dataclass(kw_only=True)
class StoreConfig:
    dlr_timeout_hours: int = 48  # a sent message with no receipt by then: expired
    retention_days: int = 7  # a finished message is deleted this long after creation


@dataclasses.dataclass(kw_only=True)
class Settings:
    http: HTTPConfig = dataclasses.field(default_factory=HTTPConfig)
    kannel: KannelConfig = dataclasses.field(default_factory=KannelConfig)
    # SMSC name -> its bind; none: every submit is refused as unroutable
    smsc: dict[str, SMPPClientConfig] = dataclasses.field(default_factory=dict)
    routing: RoutingConfig = dataclasses.field(default_factory=RoutingConfig)
    delivery: DeliveryConfig = dataclasses.field(default_factory=DeliveryConfig)
    mo: MOConfig = dataclasses.field(default_factory=MOConfig)
    store: StoreConfig = dataclasses.field(default_factory=StoreConfig)

    # PostgreSQL DSN (postgresql+asyncpg://...) or a SQLite file
    # (sqlite+aiosqlite:////abs/path.db); required to start the gateway
    db: tp.Optional[str] = None

    log_level: str = 'INFO'  # a logging level name
    log_format: str = 'text'  # or 'json': one JSON object per line


def load_settings(path: str | os.PathLike[str] = 'config/config.toml') -> Settings:
    """
    Read the `[porth]` table of the TOML file at `path` (other tables are ignored).
    An unknown key, a wrong type, a missing required key, an unknown log_level or
    log_format, or a store window, http.callback_retry_minutes, delivery.queue_limit or
    an SMSC's fallback_after, reconnect_delay, enquire_link_interval, bind_timeout or
    response_timeout below 1, or a fallback naming no other SMSC, raises ValueError
    naming the key.
    """
    data = monobase.config.read_config(os.fspath(path))
    if not isinstance(data.get('porth'), dict):
        raise ValueError(f'{path}: no [porth] table')
    settings = _build(Settings, data['porth'], 'porth')
    if settings.log_level not in logging.getLevelNamesMapping():
        raise ValueError(f'invalid porth.log_level: {settings.log_level!r}')
    if settings.log_format not in ('text', 'json'):
        raise ValueError(f'invalid porth.log_format: {settings.log_format!r}')
    for f in dataclasses.fields(StoreConfig):
        if getattr(settings.store, f.name) < 1:
            raise ValueError(f'invalid porth.store.{f.name}: must be at least 1')
    if settings.http.callback_retry_minutes < 1:
        raise ValueError(
            'invalid porth.http.callback_retry_minutes: must be at least 1'
        )
    if settings.delivery.queue_limit is not None and settings.delivery.queue_limit < 1:
        raise ValueError('invalid porth.delivery.queue_limit: must be at least 1')
    for name, smsc in settings.smsc.items():
        if smsc.tls_ca_file is not None and not smsc.tls:
            raise ValueError(
                f'invalid porth.smsc.{name}.tls_ca_file: set tls = true to use it'
            )
        if smsc.fallback is not None and smsc.fallback not in settings.smsc:
            raise ValueError(
                f'invalid porth.smsc.{name}.fallback: no SMSC named {smsc.fallback!r}'
            )
        if smsc.fallback == name:
            raise ValueError(f'invalid porth.smsc.{name}.fallback: names itself')
        for key in (
            'fallback_after',
            'reconnect_delay',
            'enquire_link_interval',
            'bind_timeout',
            'response_timeout',
        ):
            if getattr(smsc, key) < 1:
                raise ValueError(f'invalid porth.smsc.{name}.{key}: must be at least 1')
    tokens: dict[str, str] = {}
    for name, http_user in settings.http.users.items():
        key = f'porth.http.users.{name}.token'
        if len(http_user.token) < 16:
            raise ValueError(f'invalid {key}: shorter than 16 characters')
        if http_user.token in tokens:
            raise ValueError(f'invalid {key}: the same as {tokens[http_user.token]}')
        tokens[http_user.token] = key
    users: list[tuple[str, HTTPUser | KannelUser]] = [
        *((f'porth.http.users.{n}', u) for n, u in settings.http.users.items()),
        *((f'porth.kannel.users.{n}', u) for n, u in settings.kannel.users.items()),
    ]
    for key, user in users:
        for entry in user.allow_ip:
            try:
                ipaddress.ip_network(entry)
            except ValueError:
                raise ValueError(f'invalid {key}.allow_ip: {entry!r}') from None
    return settings


T = tp.TypeVar('T', bound='DataclassInstance')


def _build(cls: type[T], data: dict[str, tp.Any], path: str) -> T:
    fields = dataclasses.fields(cls)
    unknown = data.keys() - {f.name for f in fields}
    if unknown:
        raise ValueError(
            f'unknown setting(s): {sorted(f"{path}.{k}" for k in unknown)}'
        )
    for f in fields:
        no_default = (
            dataclasses.MISSING is f.default
            and dataclasses.MISSING is f.default_factory
        )
        if no_default and f.name not in data:
            raise ValueError(f'{path}.{f.name} is required')
    hints = tp.get_type_hints(cls)
    return cls(**{k: _coerce(f'{path}.{k}', hints[k], v) for k, v in data.items()})


def _coerce(key: str, hint: tp.Any, value: tp.Any) -> tp.Any:
    """Check a TOML value against `hint`; no conversion, anything else is ValueError."""
    if type(None) in tp.get_args(hint):  # Optional[X]: the key is present, so X
        (hint,) = (arg for arg in tp.get_args(hint) if arg is not type(None))
    if tp.get_origin(hint) is dict:  # a table of str -> X
        if isinstance(value, dict):
            _, item = tp.get_args(hint)
            return {k: _coerce(f'{key}.{k}', item, v) for k, v in value.items()}
        hint = dict
    elif tp.get_origin(hint) is list:  # an array of X
        if isinstance(value, list):
            (item,) = tp.get_args(hint)
            return [_coerce(f'{key}[{i}]', item, v) for i, v in enumerate(value)]
        hint = list
    elif isinstance(hint, type) and dataclasses.is_dataclass(hint):
        if isinstance(value, dict):
            return _build(hint, value, key)
    elif hint is bool:
        if isinstance(value, bool):
            return value
    elif hint is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif hint is str and isinstance(value, str):
        return value
    table = hint is dict or dataclasses.is_dataclass(hint)
    expected = 'a table' if table else 'a list' if hint is list else hint.__name__
    raise ValueError(f'invalid {key}: {value!r} is not {expected}')
