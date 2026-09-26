"""Unit tests for load_settings (env, .env and YAML resolution)."""

import pytest

from porth.config.settings import load_settings


def test_defaults_without_env(tmp_path):
    settings = load_settings({}, dotenv=tmp_path / '.env')
    assert settings.http.port == 8080
    assert settings.smpp.clients == []
    assert settings.delivery.max_retries == 3


def test_env_nests_coerces_and_parses_json_lists(tmp_path):
    env = {
        'PORTH_HTTP__PORT': '9000',
        'porth_debug': 'true',
        'PORTH_SMPP__CLIENTS': '[{"host": "smsc", "system_id": "p", "password": "s"}]',
        'HOME': '/ignored',
    }
    settings = load_settings(env, dotenv=tmp_path / '.env')
    assert settings.http.port == 9000
    assert settings.debug is True
    (client,) = settings.smpp.clients
    assert (client.host, client.port, client.system_id) == ('smsc', 2775, 'p')


def test_env_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / '.env'
    dotenv.write_text('# comment\nPORTH_HTTP__PORT=7000\nPORTH_LOG_LEVEL="DEBUG"\n')
    settings = load_settings({'PORTH_HTTP__PORT': '7001'}, dotenv=dotenv)
    assert settings.http.port == 7001
    assert settings.log_level == 'DEBUG'


def test_yaml_wins_over_env_key_by_key(tmp_path):
    config = tmp_path / 'porth.yml'
    config.write_text('http:\n  port: 8081\ndelivery:\n  retry_delay: 1\n')
    env = {
        'PORTH_CONFIG_FILE': str(config),
        'PORTH_HTTP__HOST': '10.0.0.1',  # kept: the file's http section has no host
        'PORTH_HTTP__PORT': '9000',  # overridden by the file
        'PORTH_SMPP__CLIENTS': '[{"host": "smsc", "system_id": "p", "password": "s"}]',
    }
    settings = load_settings(env, dotenv=tmp_path / '.env')
    assert (settings.http.host, settings.http.port) == ('10.0.0.1', 8081)
    assert settings.delivery.retry_delay == 1
    assert settings.smpp.clients[0].host == 'smsc'  # no smpp section in the file


def test_yaml_list_replaces_env_list(tmp_path):
    config = tmp_path / 'porth.yml'
    config.write_text('smpp:\n  clients: []\n')
    env = {
        'PORTH_CONFIG_FILE': str(config),
        'PORTH_SMPP__CLIENTS': '[{"host": "smsc", "system_id": "p", "password": "s"}]',
    }
    assert load_settings(env, dotenv=tmp_path / '.env').smpp.clients == []


@pytest.mark.parametrize(
    'env',
    [{'PORTH_HTTP__PROT': '1'}, {'PORTH_SMPP__CLIENTS': '[{"host": "h"}]'}],
)
def test_bad_keys_raise(tmp_path, env):
    with pytest.raises((ValueError, TypeError)):
        load_settings(env, dotenv=tmp_path / '.env')


@pytest.mark.parametrize(
    'yaml_text',
    [
        'smpp:\n  clients: [{host: h, system_id: p, password: }]\n',  # null password
        'http:\n  host: 123\n',
        'http:\n  port: 80.5\n',
        'http:\n  port: true\n',
        'http:\n',  # null section
        'debug: 1\n',
    ],
)
def test_wrong_yaml_types_raise(tmp_path, yaml_text):
    config = tmp_path / 'porth.yml'
    config.write_text(yaml_text)
    with pytest.raises(ValueError, match='invalid'):
        load_settings({'PORTH_CONFIG_FILE': str(config)}, dotenv=tmp_path / '.env')


@pytest.mark.parametrize('name', ['development', 'production', 'test'])
def test_shipped_configs_load(tmp_path, name):
    env = {'PORTH_CONFIG_FILE': f'config/{name}.yml'}
    assert load_settings(env, dotenv=tmp_path / '.env').config_file
