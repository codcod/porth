"""Settings: PORTH_* environment variables (and .env), or a YAML file."""

import dataclasses
import json
import os
import typing as tp
from pathlib import Path

import yaml


@dataclasses.dataclass(kw_only=True)
class HTTPConfig:
    host: str = '0.0.0.0'
    port: int = 8080


@dataclasses.dataclass(kw_only=True)
class SMPPClientConfig:
    host: str
    port: int = 2775
    system_id: str
    password: str
    system_type: str = ''


@dataclasses.dataclass(kw_only=True)
class SMPPConfig:
    clients: list[SMPPClientConfig] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(kw_only=True)
class DeliveryConfig:
    max_retries: int = 3
    retry_delay: int = 5  # seconds
    worker_count: int = 10


@dataclasses.dataclass(kw_only=True)
class Settings:
    http: HTTPConfig = dataclasses.field(default_factory=HTTPConfig)
    smpp: SMPPConfig = dataclasses.field(default_factory=SMPPConfig)
    delivery: DeliveryConfig = dataclasses.field(default_factory=DeliveryConfig)

    # Configuration and logging
    config_file: tp.Optional[str] = None
    log_level: str = 'INFO'
    debug: bool = False


def load_settings(
    env: tp.Mapping[str, str] = os.environ, dotenv: Path = Path('.env')
) -> Settings:
    """
    Read PORTH_* variables (over .env; nested keys joined by '__', lists as JSON). If
    PORTH_CONFIG_FILE names a YAML file, every key it sets wins over the environment, key
    by key (a list is taken whole). Unknown keys raise ValueError.
    """
    data = _nest({**_read_dotenv(dotenv), **env})
    if data.get('config_file'):
        with open(data['config_file']) as f:
            _merge(data, yaml.safe_load(f) or {})
    return _build(Settings, data)


def _merge(base: dict[str, tp.Any], over: tp.Mapping[str, tp.Any]) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    pairs = (
        line.split('=', 1)
        for line in path.read_text().splitlines()
        if '=' in line and not line.lstrip().startswith('#')
    )
    return {k.strip(): v.strip().strip('"\'') for k, v in pairs}


def _nest(env: tp.Mapping[str, str]) -> dict[str, tp.Any]:
    data: dict[str, tp.Any] = {}
    for key, value in env.items():
        key = key.lower()
        if not key.startswith('porth_'):
            continue
        *parents, leaf = key.removeprefix('porth_').split('__')
        node = data
        for parent in parents:
            node = node.setdefault(parent, {})
        node[leaf] = value
    return data


T = tp.TypeVar('T')


def _build(cls: type[T], data: tp.Any) -> T:
    if isinstance(data, str):
        data = json.loads(data)
    hints = tp.get_type_hints(cls)
    unknown = data.keys() - hints.keys()
    if unknown:
        raise ValueError(f'unknown {cls.__name__} setting(s): {sorted(unknown)}')
    return cls(**{key: _coerce(key, hints[key], value) for key, value in data.items()})


def _coerce(key: str, hint: tp.Any, value: tp.Any) -> tp.Any:
    """Convert an env string or YAML value to `hint`; anything else is a ValueError."""
    if type(None) in tp.get_args(hint):  # Optional[X]
        if value is None:
            return None
        (hint,) = (arg for arg in tp.get_args(hint) if arg is not type(None))
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        if isinstance(value, (dict, str)):
            return _build(hint, value)
    elif tp.get_origin(hint) is list:
        items = json.loads(value) if isinstance(value, str) else value
        if isinstance(items, list):
            (item,) = tp.get_args(hint)
            return [_coerce(key, item, v) for v in items]
    elif hint is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
    elif hint is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lstrip('+-').isdigit():
            return int(value)
    elif hint is str and isinstance(value, str):
        return value
    raise ValueError(f'invalid {key!r} setting: {value!r} is not {hint}')
