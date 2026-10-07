"""Unit tests for load_settings (the [porth] table of a TOML file)."""

from pathlib import Path

import pytest

from porth.config.settings import load_settings


def load(tmp_path, text):
    config = tmp_path / 'config.toml'
    config.write_text(text)
    return load_settings(config)


def test_minimal_table_gives_defaults(tmp_path):
    settings = load(tmp_path, '[porth]\n')
    assert settings.http.port == 8080
    assert settings.kannel.port == 13013
    assert settings.smsc == {}
    assert (settings.routing.default, settings.routing.prefixes) == (None, {})
    assert settings.delivery.max_retries == 3
    assert settings.mo.reply is True
    assert (settings.store.dlr_timeout_hours, settings.store.retention_days) == (48, 7)
    assert (settings.db, settings.log_level) == (None, 'INFO')


def test_full_file_round_trips(tmp_path):
    settings = load(
        tmp_path,
        """
[porth]
db = "postgresql+asyncpg://u:p@h/porth"
log_level = "WARNING"
[porth.http]
host = "127.0.0.1"
port = 9000
[porth.kannel]
host = "127.0.0.2"
port = 13100
default_sender = "12345"
[porth.smsc.op-a]
host = "smsc"
port = 2776
system_id = "p"
password = "s"
system_type = "VMA"
throughput = 50
[porth.smsc.op-b]
host = "smsc-b"
system_id = "q"
password = "t"
[porth.routing]
default = "op-a"
[porth.routing.prefixes]
"4470" = "op-b"
[porth.delivery]
max_retries = 1
retry_delay = 2
backoff_factor = 1
max_retry_delay = 60
worker_count = 4
[porth.mo]
url = "http://app/mo?from=%p"
reply = false
[porth.store]
dlr_timeout_hours = 24
retention_days = 30
[traffic]
ignored = true
""",
    )
    assert settings.db == 'postgresql+asyncpg://u:p@h/porth'
    assert settings.log_level == 'WARNING'
    assert (settings.http.host, settings.http.port) == ('127.0.0.1', 9000)
    kannel = settings.kannel
    assert (kannel.host, kannel.port, kannel.default_sender) == (
        '127.0.0.2',
        13100,
        '12345',
    )
    assert list(settings.smsc) == ['op-a', 'op-b']
    a, b = settings.smsc['op-a'], settings.smsc['op-b']
    assert (a.host, a.port, a.system_id, a.password) == ('smsc', 2776, 'p', 's')
    assert (a.system_type, a.throughput) == ('VMA', 50)
    assert (b.host, b.port, b.system_type, b.throughput) == ('smsc-b', 2775, '', None)
    assert settings.routing.default == 'op-a'
    assert settings.routing.prefixes == {'4470': 'op-b'}
    d = settings.delivery
    assert (d.max_retries, d.retry_delay, d.backoff_factor) == (1, 2, 1)
    assert (d.max_retry_delay, d.worker_count) == (60, 4)
    assert (settings.mo.url, settings.mo.reply) == ('http://app/mo?from=%p', False)
    assert (settings.store.dlr_timeout_hours, settings.store.retention_days) == (24, 30)


def test_host_only_kannel_keeps_kannel_default_port(tmp_path):
    kannel = load(tmp_path, '[porth.kannel]\nhost = "127.0.0.1"\n').kannel
    assert (kannel.host, kannel.port) == ('127.0.0.1', 13013)


@pytest.mark.parametrize(
    ('text', 'key'),
    [
        ('[porth]\ndebug = true\n', 'porth.debug'),
        ('[porth.smpp.client]\nhost = "h"\n', 'porth.smpp'),
        ('[porth.delivery]\nthroughput = 50\n', 'porth.delivery.throughput'),
        ('[porth.store]\nretention = 7\n', 'porth.store.retention'),
        (
            '[porth.smsc.a]\nhost = "h"\nsystem_id = "p"\npassword = "s"\nx = 1\n',
            'porth.smsc.a.x',
        ),
    ],
)
def test_unknown_key_raises_naming_it(tmp_path, text, key):
    with pytest.raises(ValueError, match=f'unknown.*{key}'):
        load(tmp_path, text)


@pytest.mark.parametrize(
    ('text', 'key'),
    [
        ('[porth.http]\nport = "8080"\n', 'porth.http.port'),
        ('[porth.delivery]\nworker_count = true\n', 'porth.delivery.worker_count'),
        (
            '[porth.smsc.a]\nhost = "h"\nsystem_id = "p"\npassword = "s"\n'
            'port = 2775.0\n',
            'porth.smsc.a.port',
        ),
        ('[porth]\nsmsc = 1\n', 'porth.smsc'),
        ('[porth.smsc]\na = 1\n', 'porth.smsc.a'),
        ('[porth.routing.prefixes]\n"30" = 1\n', 'porth.routing.prefixes.30'),
        ('[porth.mo]\nreply = "yes"\n', 'porth.mo.reply'),
        ('[porth.kannel]\ndefault_sender = 12345\n', 'porth.kannel.default_sender'),
        ('[porth]\nhttp = 1\n', 'porth.http'),
        (
            '[porth.kannel.users.u]\npassword = "p"\nallow_ip = "x"\n',
            'porth.kannel.users.u.allow_ip',
        ),
        (
            '[porth.kannel.users.u]\npassword = "p"\nallow_ip = [1]\n',
            r'porth.kannel.users.u.allow_ip\[0\]',
        ),
    ],
)
def test_wrong_type_raises_naming_key(tmp_path, text, key):
    with pytest.raises(ValueError, match=f'invalid {key}:'):
        load(tmp_path, text)


def test_missing_required_key_raises_naming_it(tmp_path):
    text = '[porth.smsc.a]\nsystem_id = "p"\npassword = "s"\n'
    with pytest.raises(ValueError, match='porth.smsc.a.host is required'):
        load(tmp_path, text)


def test_kannel_users_load(tmp_path):
    text = (
        '[porth.kannel.users.u]\npassword = "p"\nallow_ip = ["10.0.0.0/8", "::1"]\n'
        '[porth.kannel.users.v]\npassword = "q"\n'
    )
    users = load(tmp_path, text).kannel.users
    assert (users['u'].password, users['u'].allow_ip) == ('p', ['10.0.0.0/8', '::1'])
    assert (users['v'].password, users['v'].allow_ip) == ('q', [])
    assert load(tmp_path, '[porth]\n').kannel.users == {}


def test_kannel_user_without_password_raises(tmp_path):
    with pytest.raises(ValueError, match='porth.kannel.users.u.password is required'):
        load(tmp_path, '[porth.kannel.users.u]\nallow_ip = []\n')


@pytest.mark.parametrize('entry', ['10.0.0.1/8', 'nope'])
def test_invalid_allow_ip_raises(tmp_path, entry):
    text = f'[porth.kannel.users.u]\npassword = "p"\nallow_ip = ["{entry}"]\n'
    with pytest.raises(ValueError, match='invalid porth.kannel.users.u.allow_ip'):
        load(tmp_path, text)


def test_no_porth_table_raises(tmp_path):
    with pytest.raises(ValueError, match=r'no \[porth\] table'):
        load(tmp_path, '[traffic]\nx = 1\n')


def test_unknown_log_level_raises(tmp_path):
    with pytest.raises(ValueError, match='porth.log_level'):
        load(tmp_path, '[porth]\nlog_level = "LOUD"\n')


@pytest.mark.parametrize('key', ['dlr_timeout_hours', 'retention_days'])
def test_store_window_below_one_raises(tmp_path, key):
    with pytest.raises(
        ValueError, match=f'invalid porth.store.{key}: must be at least 1'
    ):
        load(tmp_path, f'[porth.store]\n{key} = 0\n')


def test_shipped_config_loads():
    root = Path(__file__).resolve().parents[3]
    settings = load_settings(root / 'config' / 'config.toml')
    assert settings.db and settings.log_level == 'DEBUG'
