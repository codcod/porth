"""Unit tests for SMSGateway listener lifecycle."""

import asyncio
import pathlib
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from porth.config.settings import (
    DeliveryConfig,
    HTTPConfig,
    KannelConfig,
    RoutingConfig,
    Settings,
    SMPPClientConfig,
)
from porth.core.message import MessageStatus, SMSMessage
from porth.main import SMSGateway

CONFIG = pathlib.Path(__file__).resolve().parents[3] / 'config' / 'config.toml'


@pytest.mark.asyncio
async def test_failed_bind_leaves_runner_for_stop_to_clean_up(uow_factory):
    with socket.socket() as taken:
        taken.bind(('127.0.0.1', 0))
        taken.listen()
        port = taken.getsockname()[1]
        gateway = SMSGateway(
            Settings(
                http=HTTPConfig(host='127.0.0.1', port=0),
                kannel=KannelConfig(host='127.0.0.1', port=port),
            ),
            uow_factory,
        )
        with pytest.raises(OSError):
            await gateway.start()
        # the Kannel runner is tracked despite its failed bind
        assert len(gateway.servers) == 2
        await gateway.stop()
    assert all(runner.server is None for runner in gateway.servers)


def test_no_db_and_no_factory_raises():
    with pytest.raises(ValueError, match='db is not set'):
        SMSGateway(Settings())


def stored(repo, status, minutes_ago, smsc='a', to='306900000001', priority='normal'):
    message = SMSMessage(
        source_addr='A',
        destination_addr=to,
        message_text=status.value,
        protocol='http',
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        smsc=smsc,
        priority=priority,
    )
    repo.messages[message.message_id] = message
    return message


def smsc(**kwargs) -> SMPPClientConfig:
    return SMPPClientConfig(host='127.0.0.1', system_id='p', password='s', **kwargs)


def two_smscs(**kwargs) -> Settings:
    """SMSCs a and b: 30... goes out through a, 44... through b."""
    return Settings(
        http=HTTPConfig(host='127.0.0.1', port=0),
        kannel=KannelConfig(host='127.0.0.1', port=0),
        smsc={'a': smsc(), 'b': smsc()},
        routing=RoutingConfig(prefixes={'30': 'a', '44': 'b'}),
        **kwargs,
    )


def quiet(gateway, monkeypatch):
    """No workers and no binds: what start() queues stays on the queues to be read."""
    for engine in gateway.engines.values():
        monkeypatch.setattr(engine, 'start', _noop)
    for client in gateway.smpp_clients:
        monkeypatch.setattr(client, 'start', lambda: None)


def drain(queue) -> list[str]:
    return [queue._queue.get_nowait()[2].message_id for _ in range(queue.qsize())]


@pytest.mark.asyncio
async def test_start_requeues_unsent_oldest_first_and_resumes_callbacks(
    uow_factory, monkeypatch, caplog
):
    repo = uow_factory.repo
    newer = stored(repo, MessageStatus.QUEUED, 1)
    older = stored(repo, MessageStatus.PENDING, 5)
    for status in (MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.FAILED):
        stored(repo, status, 3)
    done = stored(repo, MessageStatus.DELIVERED, 2)
    repo.dlr_callbacks[done.message_id] = ('http://127.0.0.1:1/dlr', None)

    gateway = SMSGateway(two_smscs(), uow_factory)
    resumed = []
    monkeypatch.setattr(
        gateway.dlr_handler, '_fetch', lambda *args: _record(resumed, args)
    )
    quiet(gateway, monkeypatch)
    await gateway.start()
    try:
        assert drain(gateway.queues['a']) == [older.message_id, newer.message_id]
        assert gateway.queues['b'].empty()
        assert 'Re-queued 2 message(s) from the store' in caplog.text
        await asyncio.sleep(0)
        assert resumed == [(done.message_id, 'http://127.0.0.1:1/dlr', None)]
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_recovery_puts_high_priority_first(uow_factory, monkeypatch):
    repo = uow_factory.repo
    normal = stored(repo, MessageStatus.QUEUED, 5)
    high = stored(repo, MessageStatus.QUEUED, 1, priority='high')

    gateway = SMSGateway(two_smscs(), uow_factory)
    quiet(gateway, monkeypatch)
    await gateway.start()
    try:
        assert drain(gateway.queues['a']) == [high.message_id, normal.message_id]
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_recovery_reroutes_a_message_whose_smsc_is_gone(uow_factory, monkeypatch):
    repo = uow_factory.repo
    kept = stored(repo, MessageStatus.QUEUED, 4, smsc='a', to='447000000001')
    removed = stored(repo, MessageStatus.QUEUED, 3, smsc='gone', to='447000000002')
    unrouted = stored(repo, MessageStatus.QUEUED, 2, smsc=None, to='306900000003')
    lost = stored(repo, MessageStatus.QUEUED, 1, smsc='gone', to='15550000004')

    gateway = SMSGateway(two_smscs(), uow_factory)
    quiet(gateway, monkeypatch)
    await gateway.start()
    try:
        # a kept message stays on its SMSC, even where a prefix now says b
        assert drain(gateway.queues['a']) == [kept.message_id, unrouted.message_id]
        assert drain(gateway.queues['b']) == [removed.message_id]
        assert repo.messages[removed.message_id].smsc == 'b'
        assert repo.messages[unrouted.message_id].smsc == 'a'
        assert repo.messages[lost.message_id].status == MessageStatus.FAILED
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_recovery_failed_stores_its_callback_and_resume_makes_it_once(
    uow_factory, monkeypatch
):
    lost = stored(uow_factory.repo, MessageStatus.QUEUED, 1, smsc='gone', to='1555')
    lost.callback_url = 'http://m/cb'
    gateway = SMSGateway(two_smscs(), uow_factory)
    made = []
    monkeypatch.setattr(
        gateway.dlr_handler, '_fetch', lambda *args: _record(made, args)
    )
    quiet(gateway, monkeypatch)
    await gateway.start()
    try:
        await asyncio.sleep(0)
        ((message_id, url, body),) = made
        assert (message_id, url) == (lost.message_id, 'http://m/cb')
        assert body['status'] == 'failed'
    finally:
        await gateway.stop()


class Blocking:
    async def send_message(self, message):
        await asyncio.Event().wait()


class Instant:
    async def send_message(self, message):
        return {'smsc_message_ids': [f'id-{message.message_id}']}


@pytest.mark.asyncio
async def test_a_stuck_smsc_holds_up_only_its_own_messages(uow_factory):
    gateway = SMSGateway(
        two_smscs(delivery=DeliveryConfig(worker_count=2)), uow_factory
    )
    gateway.engines['a'].smpp_client = Blocking()  # type: ignore[assignment]
    gateway.engines['b'].smpp_client = Instant()  # type: ignore[assignment]
    repo = uow_factory.repo
    for engine in gateway.engines.values():
        await engine.start()
    try:
        for to in ('306900000001', '306900000002', '306900000003'):
            message = stored(repo, MessageStatus.QUEUED, 0, 'a', to)
            await gateway.queues['a'].put(message)
        for_b = stored(repo, MessageStatus.QUEUED, 0, 'b', '447000000001')
        await gateway.queues['b'].put(for_b)
        async with asyncio.timeout(2):
            while repo.messages[for_b.message_id].status != MessageStatus.SENT:
                await asyncio.sleep(0.01)
        assert gateway.queues['a'].qsize() == 1  # both of a's workers are stuck
    finally:
        for engine in gateway.engines.values():
            await engine.stop()


def test_each_smsc_gets_its_own_throughput(uow_factory):
    settings = two_smscs()
    settings.smsc['a'].throughput = 5
    gateway = SMSGateway(settings, uow_factory)
    a, b = (gateway.engines[n].smpp_client for n in ('a', 'b'))
    assert a is not None and a._pacer is not None and a._pacer.rate == 5
    assert b is not None and b._pacer is None
    assert gateway.smpp_clients == [a, b]


def test_zero_throughput_fails_the_gateway(uow_factory):
    settings = two_smscs()
    settings.smsc['b'].throughput = 0
    with pytest.raises(ValueError, match='porth.smsc.b.throughput'):
        SMSGateway(settings, uow_factory)


def test_bad_routing_fails_the_gateway(uow_factory):
    settings = two_smscs()
    settings.routing.default = 'zz'
    with pytest.raises(ValueError, match='porth.routing.default'):
        SMSGateway(settings, uow_factory)


def _config_check(path):
    # what `make config-check` runs
    return subprocess.run(
        [sys.executable, '-m', 'porth.main', '--check', str(path)],
        capture_output=True,
        text=True,
    )


def test_config_check_accepts_the_shipped_config():
    assert _config_check(CONFIG).returncode == 0


@pytest.mark.parametrize(
    'extra, key',
    [
        ('[porth.routing.prefixes]\n"4a" = "local"\n', 'porth.routing.prefixes."4a"'),
        ('[porth.routing.prefixes]\n"30" = "nope"\n', 'porth.routing.prefixes."30"'),
        ('[porth.routing]\ndefault = "nope"\n', 'porth.routing.default'),
        (
            '[porth.smsc.""]\nhost = "h"\nsystem_id = "s"\npassword = "p"\n',
            'porth.smsc.""',
        ),
        ('', 'porth.smsc.local.throughput'),
    ],
)
def test_config_check_fails_as_startup_does(tmp_path, extra, key):
    smsc = (
        '[porth.smsc.local]\nhost = "h"\nport = 2775\nsystem_id = "s"\npassword = "p"\n'
    )
    if not extra:
        smsc += 'throughput = 0\n'
    config = tmp_path / 'config.toml'
    config.write_text(CONFIG.read_text() + smsc + extra)
    result = _config_check(config)
    assert result.returncode != 0
    assert key in result.stderr


async def _noop():
    pass


async def _record(into, args):
    into.append(args)
