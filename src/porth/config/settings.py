"""Settings: the `[porth]` table of a TOML file (design.md §4.3)."""

import dataclasses
import logging
import os
import typing as tp

import monobase.config


@dataclasses.dataclass(kw_only=True)
class HTTPConfig:
    host: str = '0.0.0.0'
    port: int = 8080


@dataclasses.dataclass(kw_only=True)
class KannelConfig:
    host: str = '0.0.0.0'
    port: int = 13013  # smsbox's sendsms default
    default_sender: tp.Optional[str] = None  # Kannel's global-sender


@dataclasses.dataclass(kw_only=True)
class SMPPClientConfig:
    host: str
    port: int = 2775
    system_id: str
    password: str
    system_type: str = ''


@dataclasses.dataclass(kw_only=True)
class SMPPConfig:
    client: tp.Optional[SMPPClientConfig] = None  # unset: no bind


@dataclasses.dataclass(kw_only=True)
class DeliveryConfig:
    max_retries: int = 3
    retry_delay: int = 5  # seconds before the first retry
    # ponytail: ints, since the loader takes no floats; add a float branch to _coerce if
    # someone needs them
    backoff_factor: int = 2  # 1: fixed retry_delay
    max_retry_delay: int = 300  # seconds
    worker_count: int = 10
    # ponytail: int, so at least 1/s; sub-1 TPS needs a float and a bucket of >= 1 token
    throughput: tp.Optional[int] = None  # submit_sm PDUs/s; unset = unlimited


@dataclasses.dataclass(kw_only=True)
class MOConfig:
    # Kannel get-url template; unset: each MO is logged and dropped
    url: tp.Optional[str] = None
    reply: bool = True  # False: Kannel's max-messages = 0


@dataclasses.dataclass(kw_only=True)
class Settings:
    http: HTTPConfig = dataclasses.field(default_factory=HTTPConfig)
    kannel: KannelConfig = dataclasses.field(default_factory=KannelConfig)
    smpp: SMPPConfig = dataclasses.field(default_factory=SMPPConfig)
    delivery: DeliveryConfig = dataclasses.field(default_factory=DeliveryConfig)
    mo: MOConfig = dataclasses.field(default_factory=MOConfig)

    # PostgreSQL DSN (postgresql+asyncpg://...); required to start the gateway
    db: tp.Optional[str] = None

    log_level: str = 'INFO'  # a logging level name


def load_settings(path: str | os.PathLike[str] = 'config/config.toml') -> Settings:
    """
    Read the `[porth]` table of the TOML file at `path` (other tables are ignored).
    An unknown key, a wrong type, a missing required key or an unknown log_level
    raises ValueError naming the key.
    """
    data = monobase.config.read_config(os.fspath(path))
    if not isinstance(data.get('porth'), dict):
        raise ValueError(f'{path}: no [porth] table')
    settings = _build(Settings, data['porth'], 'porth')
    if settings.log_level not in logging.getLevelNamesMapping():
        raise ValueError(f'invalid porth.log_level: {settings.log_level!r}')
    return settings


T = tp.TypeVar('T')


def _build(cls: type[T], data: dict[str, tp.Any], path: str) -> T:
    fields = dataclasses.fields(cls)  # type: ignore[arg-type]
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
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
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
    expected = 'a table' if dataclasses.is_dataclass(hint) else hint.__name__
    raise ValueError(f'invalid {key}: {value!r} is not {expected}')
