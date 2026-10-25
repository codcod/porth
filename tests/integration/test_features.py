"""End-to-end tests of porth's five headline features (POR-031, POR-053).

Each test starts porth as bin/porth does, from a config file, and drives it only from
outside: the REST and Kannel APIs, smppai SMSCs on real sockets, callback servers and
/metrics. Nothing reads porth's objects or its database rows; the database is only
emptied before each test, so no earlier run's unsent message is sent here.
"""

import asyncio
import contextlib
import dataclasses
import datetime as dt
import json
import re
import time
import uuid

import aiohttp
import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDictProxy
from prometheus_client.parser import text_string_to_metric_families
from smpp import NpiType, SMPPServer, TonType

from porth.adapters.db import make_engine
from porth.adapters.tables import dlr_callbacks, messages, smsc_ids
from porth.config.settings import load_settings
from porth.entrypoints.app import build_app
from tests.integration.conftest import DSN
from tests.integration.test_repository import pytestmark  # noqa: F401
from tests.integration.test_restart import until
from tests.integration.test_smpp_flow import free_port


@dataclasses.dataclass
class Submit:
    at: float  # time.monotonic() on arrival
    bind: str  # the session's bind type: 'transceiver', 'transmitter'
    pdu: object
    smsc_id: str  # the id answered, '' for a rejected submit


class SMSC:
    """An smppai SMSC recording every submit_sm. It answers each with the id
    <name>-<n>, or refuses it (ESME_RSUBMITFAIL) while reject is set."""

    def __init__(self, name: str, extra: str = '') -> None:
        self.name = name
        self.port = free_port()
        self.extra = extra  # more keys for its [porth.smsc.<name>] table
        self.submits: list[Submit] = []
        self.reject = False
        self.server: SMPPServer | None = None

    def toml(self) -> str:
        return (
            f'[porth.smsc.{self.name}]\nhost = "127.0.0.1"\nport = {self.port}\n'
            f'system_id = "porth"\npassword = "pw"\n{self.extra}\n'
        )

    async def start(self) -> None:
        self.server = SMPPServer(
            host='127.0.0.1', port=self.port, setup_signal_handlers=False
        )
        # A stop waits 30 s + 10 s by default for a bound porth to leave
        self.server.configure_shutdown(grace_period=0.1, reminder_delay=0.1)
        self.server.on_message_received = self.on_submit
        await self.server.start()

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()

    def on_submit(self, server, session, pdu) -> str:
        smsc_id = '' if self.reject else f'{self.name}-{len(self.submits) + 1}'
        self.submits.append(Submit(time.monotonic(), session.bind_type, pdu, smsc_id))
        if self.reject:
            raise RuntimeError('refused by the test')  # smppai: ESME_RSUBMITFAIL
        return smsc_id

    def texts(self) -> list[str]:
        return [s.pdu.get_message_text() for s in self.submits]

    async def receipt(self, smsc_id: str, stat: str = 'DELIVRD') -> None:
        assert self.server is not None
        assert await self.server.deliver_sm(
            'porth',
            source_addr='306900000001',
            destination_addr='1234',
            short_message=(
                f'id:{smsc_id} sub:001 dlvrd:001 submit date:2610111200 '
                f'done date:2610111201 stat:{stat} err:000 text:'
            ),
            esm_class=0x04,
        )

    async def mo(self, sender: str, to: str, text: str) -> None:
        assert self.server is not None
        assert await self.server.deliver_sm(
            'porth',
            source_addr=sender,
            source_addr_ton=TonType.INTERNATIONAL,
            source_addr_npi=NpiType.ISDN,
            destination_addr=to,
            short_message=text,
        )


@dataclasses.dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    body: bytes


class Target:
    """An HTTP server recording every request, answering each with reply()."""

    def __init__(self, reply=lambda request: web.Response()) -> None:
        self.requests: list[Request] = []
        self.reply = reply
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', self.handle)
        self.port = free_port()  # known before start, for the config that names it
        self.server = TestServer(app, host='127.0.0.1', port=self.port)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            Request(request.method, request.path, dict(request.query), body)
        )
        return self.reply(request)

    def url(self, path: str) -> str:
        return f'http://127.0.0.1:{self.port}{path}'

    async def start(self) -> None:
        await self.server.start_server()

    async def stop(self) -> None:
        await self.server.close()


class Porth:
    """porth started from a config file as bin/porth starts it, on free ports."""

    def __init__(self, tmp_path, config: str) -> None:
        self.http_port, self.kannel_port = free_port(), free_port()
        path = tmp_path / f'config-{uuid.uuid4().hex}.toml'
        path.write_text(
            f'[porth]\ndb = "{DSN}"\nlog_level = "WARNING"\n'
            f'[porth.http]\nhost = "127.0.0.1"\nport = {self.http_port}\n'
            f'[porth.kannel]\nhost = "127.0.0.1"\nport = {self.kannel_port}\n' + config
        )
        self.settings = load_settings(path)
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        self.runner = web.AppRunner(build_app(self.settings))
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', self.http_port)
        await site.start()

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    @property
    def rest(self) -> str:
        return f'http://127.0.0.1:{self.http_port}'

    async def send(self, **body) -> tuple[int, dict]:
        status, _, answer = await self.send_raw(**body)
        return status, answer

    async def send_raw(
        self, headers=None, **body
    ) -> tuple[int, CIMultiDictProxy, dict]:
        """A REST send with these request headers, answering its response headers too."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{self.rest}/api/v1/sms/send', json=body, headers=headers
            ) as r:
                return r.status, r.headers, await r.json()

    async def status(self, message_id: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'{self.rest}/api/v1/sms/status/{message_id}') as r:
                return (await r.json())['status']

    async def ready(self) -> bool:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'{self.rest}/ready') as r:
                return r.status == 200

    async def metric(self, name: str, **labels: str) -> float:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'{self.rest}/metrics') as r:
                text = await r.text()
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                if sample.name == name and labels.items() <= sample.labels.items():
                    return sample.value
        return 0.0

    async def sendsms(
        self, method: str = 'GET', params=None, headers=None, data=None
    ) -> tuple[int, str]:
        url = f'http://127.0.0.1:{self.kannel_port}/cgi-bin/sendsms'
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, params=params, headers=headers, data=data
            ) as r:
                return r.status, await r.text()


@contextlib.asynccontextmanager
async def running(*parts):
    """Start parts in order, and stop them in reverse."""
    async with contextlib.AsyncExitStack() as stack:
        for part in parts:
            await part.start()
            stack.push_async_callback(part.stop)
        yield


def message_ids(body: str) -> list[str]:
    """The Message-ID lines of an accepted sendsms answer."""
    lines = body.splitlines()
    assert lines[0] == '0: Accepted for delivery', body
    return [line.removeprefix('Message-ID: ') for line in lines[1:]]


async def _is(porth: Porth, message_id: str, expected: str) -> bool:
    return await porth.status(message_id) == expected


@pytest_asyncio.fixture
async def clean(database):
    """An empty message store, so recovery sends nothing but this test's messages."""
    engine = make_engine(DSN)
    async with engine.begin() as conn:
        for table in (dlr_callbacks, smsc_ids, messages):
            await conn.execute(sa.delete(table))
    await engine.dispose()


ROUTE_ALL = '[porth.routing]\ndefault = "op"\n'
USERS = """
[porth.kannel.users.acme]
password = "secret"
allow_ip = ["127.0.0.1"]

[porth.kannel.users.remote]
password = "secret"
allow_ip = ["10.0.0.0/8"]
"""

TOKENS = """
[porth.http.users.app]
token = "app-token-0123456789"
allow_ip = ["127.0.0.1"]

[porth.http.users.remote]
token = "remote-token-0123456789"
allow_ip = ["10.0.0.0/8"]
"""


# 1. Kannel drop-in


@pytest.mark.asyncio
async def test_kannel_client_sends_to_two_numbers_and_gets_a_dlr_url_call_for_each(
    clean, tmp_path
):
    smsc, dlr = SMSC('op'), Target()
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + USERS)
    async with running(smsc, dlr, porth):
        status, body = await porth.sendsms(
            params={
                'username': 'acme',
                'password': 'secret',
                'from': '1234',
                'to': '+306900000001 +306900000002',
                'text': 'Hello',
                'dlr-mask': '3',
                'dlr-url': dlr.url('/dlr') + '?d=%d&id=%I&to=%p&smsc_id=%F',
            }
        )
        assert status == 202
        ids = message_ids(body)
        assert len(ids) == 2

        await until(lambda: len(smsc.submits) == 2)
        for submit in smsc.submits:
            await smsc.receipt(submit.smsc_id)
        await until(lambda: len(dlr.requests) == 2)
        await asyncio.sleep(0.5)  # time enough for a second call per message

    assert all(s.pdu.data_coding == 0 for s in smsc.submits)
    assert smsc.texts() == ['Hello', 'Hello']
    got = {(r.query['to'], r.query['d'], r.query['smsc_id']) for r in dlr.requests}
    sent = {(f'+{s.pdu.destination_addr}', '1', s.smsc_id) for s in smsc.submits}
    assert got == sent
    assert {r.query['id'] for r in dlr.requests} == set(ids)


@pytest.mark.asyncio
async def test_kannel_post_is_sent_and_a_refused_caller_gets_kannels_403(
    clean, tmp_path
):
    smsc = SMSC('op')
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + USERS)
    refused = (403, 'Authorization failed for sendsms')
    async with running(smsc, porth):
        status, body = await porth.sendsms(
            'POST',
            headers={
                'X-Kannel-Username': 'acme',
                'X-Kannel-Password': 'secret',
                'X-Kannel-From': '1234',
                'X-Kannel-To': '306900000001',
                'Content-Type': 'text/plain',
            },
            data='Hello by POST',
        )
        assert status == 202
        assert len(message_ids(body)) == 1

        wrong_password = {'username': 'acme', 'password': 'nope'}
        outside_allow_ip = {'username': 'remote', 'password': 'secret'}
        for credentials in (wrong_password, outside_allow_ip):
            params = {'from': '1234', 'to': '306900000002', 'text': 'Refused'}
            status, body = await porth.sendsms(params=credentials | params)
            assert (status, body.strip()) == refused

        await until(lambda: smsc.submits)
        await asyncio.sleep(0.5)  # time enough for a refused message to go out
    assert smsc.texts() == ['Hello by POST']


# 2. Reliable delivery


@pytest.mark.asyncio
async def test_message_taken_while_the_smsc_is_down_is_retried_and_delivered(
    clean, tmp_path
):
    smsc = SMSC('op')
    delivery = '[porth.delivery]\nretry_delay = 1\nmax_retries = 5\n'
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + delivery)
    async with running(porth):
        retries = await porth.metric('porth_send_retries_total', smsc='op')
        status, body = await porth.send(
            source_addr='1234',
            destination_addr='306900000001',
            message_text='Down, then up',
        )
        assert status == 200
        message_id = body['message_id']

        async def retried() -> bool:
            now = await porth.metric('porth_send_retries_total', smsc='op')
            return now > retries

        await until(retried)
        assert await porth.status(message_id) == 'queued'

        async with running(smsc):
            await until(lambda: smsc.submits, timeout=15)
            await smsc.receipt(smsc.submits[0].smsc_id)
            await until(lambda: _is(porth, message_id, 'delivered'))
            await asyncio.sleep(0.5)  # time enough for a wrongful resend
    assert smsc.texts() == ['Down, then up']


@pytest.mark.asyncio
async def test_queued_message_survives_a_restart_and_is_sent_once(clean, tmp_path):
    smsc, app = SMSC('op'), Target()
    config = smsc.toml() + ROUTE_ALL + '[porth.delivery]\nretry_delay = 60\n'

    first = Porth(tmp_path, config)
    async with running(first):
        retries = await first.metric('porth_send_retries_total', smsc='op')
        status, body = await first.send(
            source_addr='1234',
            destination_addr='306900000001',
            message_text='Across a restart',
            callback_url=app.url('/status'),
        )
        assert status == 200
        message_id = body['message_id']

        async def retried() -> bool:
            return await first.metric('porth_send_retries_total', smsc='op') > retries

        await until(retried)  # its first attempt failed; the retry waits 60 s
        assert await first.status(message_id) == 'queued'

    second = Porth(tmp_path, config)
    async with running(smsc, app, second):
        await until(lambda: smsc.submits)
        await smsc.receipt(smsc.submits[0].smsc_id)
        await until(lambda: app.requests)
        await asyncio.sleep(0.5)  # time enough for a wrongful resend

    assert smsc.texts() == ['Across a restart']
    (request,) = app.requests
    assert request.method == 'POST'
    assert request.path == '/status'
    callback = json.loads(request.body)
    assert (callback['message_id'], callback['status']) == (message_id, 'delivered')


@pytest.mark.asyncio
async def test_message_the_smsc_rejects_fails_at_once_and_its_dlr_url_gets_16(
    clean, tmp_path
):
    smsc, dlr = SMSC('op'), Target()
    smsc.reject = True
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL)
    async with running(smsc, dlr, porth):
        retries = await porth.metric('porth_send_retries_total', smsc='op')
        status, body = await porth.sendsms(
            params={
                'from': '1234',
                'to': '306900000001',
                'text': 'Refused by the SMSC',
                'dlr-mask': '2',
                'dlr-url': dlr.url('/dlr') + '?d=%d&smsc_id=%F',
            }
        )
        assert status == 202
        (message_id,) = message_ids(body)

        await until(lambda: _is(porth, message_id, 'failed'))
        await until(lambda: dlr.requests)
        await asyncio.sleep(0.5)  # time enough for a wrongful retry
        assert await porth.metric('porth_send_retries_total', smsc='op') == retries

    assert len(smsc.submits) == 1
    assert [r.query for r in dlr.requests] == [{'d': '16', 'smsc_id': ''}]


@pytest.mark.asyncio
async def test_message_for_an_smsc_that_went_down_goes_out_through_its_fallback(
    clean, tmp_path
):
    a = SMSC('a', extra='fallback = "b"\nfallback_after = 1\nreconnect_delay = 1')
    b = SMSC('b')
    delivery = '[porth.delivery]\nretry_delay = 1\nmax_retries = 5\n'
    porth = Porth(
        tmp_path, a.toml() + b.toml() + '[porth.routing]\ndefault = "a"\n' + delivery
    )
    send = {'source_addr': '1234', 'destination_addr': '306900000001'}
    async with running(a, b, porth):
        await until(porth.ready)
        status, _ = await porth.send(message_text='Before', **send)
        assert status == 200
        await until(lambda: a.submits)

        await a.stop()
        status, body = await porth.send(message_text='After', **send)
        assert status == 200
        await until(lambda: b.submits)
        await until(lambda: _is(porth, body['message_id'], 'sent'))

    assert a.texts() == ['Before']
    assert b.texts() == ['After']


@pytest.mark.asyncio
async def test_message_whose_validity_passes_while_its_smsc_is_down_expires_unsent(
    clean, tmp_path
):
    smsc, app = SMSC('op', extra='reconnect_delay = 1'), Target()
    # the second attempt runs 4 s after the first: time to bind the SMSC once validity passes
    delivery = '[porth.delivery]\nretry_delay = 4\nmax_retries = 5\n'
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + delivery)
    async with running(app, porth):
        retries = await porth.metric('porth_send_retries_total', smsc='op')
        valid_until = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
        status, body = await porth.send(
            source_addr='1234',
            destination_addr='306900000001',
            message_text='Too late',
            valid_until=valid_until.isoformat(),
            callback_url=app.url('/status'),
        )
        assert status == 200
        message_id = body['message_id']

        async def retried() -> bool:
            return await porth.metric('porth_send_retries_total', smsc='op') > retries

        await until(retried)  # tried while valid, not expired at once
        assert await porth.status(message_id) == 'queued'

        wait = (valid_until - dt.datetime.now(dt.UTC)).total_seconds()
        await asyncio.sleep(max(wait, 0))
        async with running(smsc):
            await until(porth.ready)
            # bound now, with the message still waiting for the retry that finds it expired
            assert await porth.status(message_id) == 'queued'
            await until(lambda: _is(porth, message_id, 'expired'))
            await until(lambda: app.requests)
            await asyncio.sleep(0.5)  # time enough for a wrongful send

    assert smsc.texts() == []
    (request,) = app.requests
    callback = json.loads(request.body)
    assert (callback['message_id'], callback['status']) == (message_id, 'expired')


@pytest.mark.asyncio
async def test_past_the_queue_limit_both_apis_refuse_with_a_503(clean, tmp_path):
    smsc = SMSC('op', extra='reconnect_delay = 1')
    delivery = '[porth.delivery]\nqueue_limit = 2\nworker_count = 1\nmax_retries = 10\n'
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + delivery)
    async with running(porth):
        accepted = []
        for n in range(5):  # queue_limit + worker_count + 2
            status, headers, body = await porth.send_raw(
                source_addr='1234', destination_addr='306900000001', message_text=f'{n}'
            )
            if status != 200:
                break
            accepted.append(f'{n}')
        assert (status, headers.get('Retry-After')) == (503, '10')
        assert 2 <= len(accepted) <= 3  # a worker may hold one mid-attempt

        status, text = await porth.sendsms(
            params={'from': '1234', 'to': '306900000001', 'text': 'Kannel'}
        )
        assert (status, text.strip()) == (503, 'Temporal failure, try again later.')

        async with running(smsc):
            await until(lambda: len(smsc.submits) == len(accepted), timeout=20)
            await asyncio.sleep(0.5)  # time enough for a refused message to go out

    assert sorted(smsc.texts()) == accepted


# 3. Routing across several SMSCs


@pytest.mark.asyncio
async def test_each_number_reaches_the_smsc_its_longest_prefix_names(clean, tmp_path):
    a, b = SMSC('a'), SMSC('b')
    prefixes = '[porth.routing.prefixes]\n"4470" = "b"\n"447012" = "a"\n'
    with_default = Porth(
        tmp_path, a.toml() + b.toml() + '[porth.routing]\ndefault = "a"\n' + prefixes
    )
    async with running(a, b, with_default):
        for number in ('447012000001', '447099000001', '306900000001'):
            status, _ = await with_default.send(
                source_addr='1234', destination_addr=number, message_text=number
            )
            assert status == 200
        status, body = await with_default.sendsms(
            params={'from': '1234', 'to': '447012000002', 'text': 'b', 'smsc': 'b'}
        )
        assert status == 202
        status, body = await with_default.sendsms(
            params={'from': '1234', 'to': '306900000003', 'text': 'x', 'smsc': 'c'}
        )
        assert (status, body.strip()) == (403, 'Not routable. Do not try again.')
        await until(lambda: len(a.submits) == 2 and len(b.submits) == 2)

    assert sorted(a.texts()) == ['306900000001', '447012000001']
    assert sorted(b.texts()) == ['447099000001', 'b']

    no_default = Porth(tmp_path, a.toml() + b.toml() + prefixes)
    async with running(no_default):
        status, body = await no_default.send(
            source_addr='1234', destination_addr='15550000001', message_text='x'
        )
        assert status == 400
        status, body = await no_default.sendsms(
            params={'from': '1234', 'to': '15550000001', 'text': 'x'}
        )
        assert (status, body.strip()) == (403, 'Not routable. Do not try again.')


@pytest.mark.asyncio
async def test_smsc_never_gets_more_than_its_throughput_in_any_second(clean, tmp_path):
    smsc = SMSC('op', extra='throughput = 2')
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL)
    async with running(smsc, porth):
        await until(porth.ready)
        for n in range(6):
            status, _ = await porth.send(
                source_addr='1234', destination_addr='306900000001', message_text=f'{n}'
            )
            assert status == 200
        await until(lambda: len(smsc.submits) == 6)

    times = [s.at for s in smsc.submits]
    # any 3 consecutive submits span at least a second (a little less for jitter)
    assert all(later - earlier >= 0.9 for earlier, later in zip(times, times[2:]))


@pytest.mark.asyncio
async def test_without_transceiver_sends_on_the_transmitter_and_receipts_on_the_receiver(
    clean, tmp_path
):
    smsc = SMSC('op', extra='transceiver = false')
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL)
    async with running(smsc, porth):
        await until(porth.ready)
        assert smsc.server is not None
        binds = sorted(s.bind_type for s in smsc.server.get_client_sessions())
        assert binds == ['receiver', 'transmitter']

        status, body = await porth.send(
            source_addr='1234', destination_addr='306900000001', message_text='Split'
        )
        assert status == 200
        await until(lambda: smsc.submits)
        await smsc.receipt(smsc.submits[0].smsc_id)
        await until(lambda: _is(porth, body['message_id'], 'delivered'))

    assert [s.bind for s in smsc.submits] == ['transmitter']


# 4. REST API


@pytest.mark.asyncio
async def test_rest_submit_repeated_with_its_key_is_sent_once_and_reported_once(
    clean, tmp_path
):
    smsc, app = SMSC('op'), Target()
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL)
    key = str(uuid.uuid4())
    submit = {
        'source_addr': '1234',
        'destination_addr': '306900000001',
        'message_text': 'Once',
        'callback_url': app.url('/status'),
        'idempotency_key': key,
    }
    async with running(smsc, app, porth):
        status, first = await porth.send(**submit)
        assert (status, first['status']) == (200, 'queued')
        status, repeat = await porth.send(**submit)
        assert status == 200
        assert repeat['message_id'] == first['message_id']
        assert repeat['message'] == 'Message already accepted'

        message_id = first['message_id']
        await until(lambda: _is(porth, message_id, 'sent'))
        await smsc.receipt(smsc.submits[0].smsc_id)
        await until(lambda: _is(porth, message_id, 'delivered'))
        await until(lambda: app.requests)
        await asyncio.sleep(0.5)  # time enough for a second send or callback

    assert smsc.texts() == ['Once']
    (request,) = app.requests
    callback = json.loads(request.body)
    assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', callback.pop('occurred_at'))
    assert callback == {
        'message_id': message_id,
        'status': 'delivered',
        'idempotency_key': key,
    }


@pytest.mark.asyncio
async def test_high_priority_rest_message_overtakes_waiting_normal_ones(
    clean, tmp_path
):
    smsc = SMSC('op', extra='throughput = 2')
    delivery = '[porth.delivery]\nworker_count = 1\n'
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + delivery)
    normal = [f'normal {n}' for n in range(1, 6)]
    async with running(smsc, porth):
        await until(porth.ready)
        for text in normal:
            await porth.send(
                source_addr='1234', destination_addr='306900000001', message_text=text
            )
        await porth.send(
            source_addr='1234',
            destination_addr='306900000001',
            message_text='high',
            priority='high',
        )
        await until(lambda: len(smsc.submits) == 6)

    texts = smsc.texts()
    # a worker may already hold the first normal messages; the last two wait behind it
    assert texts[-2:] == normal[-2:]
    assert [t for t in texts if t != 'high'] == normal


@pytest.mark.asyncio
async def test_rest_api_needs_a_users_token_from_an_address_it_allows(clean, tmp_path):
    smsc = SMSC('op')
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + TOKENS)
    send = {
        'source_addr': '1234',
        'destination_addr': '306900000001',
        'message_text': 'Authorized',
    }
    async with running(smsc, porth):
        status, headers, _ = await porth.send_raw(**send)
        assert status == 401
        assert headers['WWW-Authenticate'] == 'Bearer realm="porth"'
        for token in ('wrong-token-0123456789', 'remote-token-0123456789'):
            auth = {'Authorization': f'Bearer {token}'}
            status, _, _ = await porth.send_raw(headers=auth, **send)
            assert status == 401

        auth = {'Authorization': 'Bearer app-token-0123456789'}
        status, _, _ = await porth.send_raw(headers=auth, **send)
        assert status == 200
        await until(lambda: smsc.submits)

        async with aiohttp.ClientSession() as session:
            for probe in ('/health', '/ready'):
                async with session.get(f'{porth.rest}{probe}') as r:
                    assert r.status == 200, probe  # no token

    assert smsc.texts() == ['Authorized']


# 5. Two-way and long SMS


@pytest.mark.asyncio
async def test_mo_reaches_the_application_and_its_reply_reaches_the_subscriber(
    clean, tmp_path
):
    smsc = SMSC('op')
    app = Target(lambda request: web.Response(text='Thanks'))
    mo = f'[porth.mo]\nurl = "{app.url("/mo")}?from=%p&text=%a"\n'
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL + mo)
    async with running(smsc, app, porth):
        await until(porth.ready)
        await smsc.mo('306900000001', '1234', 'Hello there')
        await until(lambda: smsc.submits)

    assert [r.query for r in app.requests] == [
        {'from': '+306900000001', 'text': 'Hello there'}
    ]
    (reply,) = smsc.submits
    assert (reply.pdu.source_addr, reply.pdu.destination_addr) == (
        '1234',
        '306900000001',
    )
    assert reply.pdu.get_message_text() == 'Thanks'


@pytest.mark.asyncio
async def test_long_messages_go_out_in_parts_and_are_delivered_on_every_part(
    clean, tmp_path
):
    smsc, app = SMSC('op'), Target()
    porth = Porth(tmp_path, smsc.toml() + ROUTE_ALL)
    gsm = ('Long GSM text. ' * 14)[:200]  # 2 parts of up to 153 characters
    ucs2 = ('Длинный текст. ' * 7)[:100]  # 2 parts of up to 67 characters
    async with running(smsc, app, porth):
        ids = {}
        for coding, text in ((0x00, gsm), (0x08, ucs2)):
            status, body = await porth.send(
                source_addr='1234',
                destination_addr='306900000001',
                message_text=text,
                callback_url=app.url(f'/status/{coding}'),
            )
            assert status == 200
            ids[coding] = body['message_id']
        await until(lambda: len(smsc.submits) == 4)

        parts: dict[int, list[Submit]] = {0x00: [], 0x08: []}
        for submit in smsc.submits:
            parts[submit.pdu.data_coding].append(submit)
        for coding, message_id in ids.items():
            udh = [s.pdu.get_concatenated_info() for s in parts[coding]]
            assert len({h.reference for h in udh}) == 1  # one message, one reference
            assert sorted((h.total_parts, h.part_number) for h in udh) == [
                (2, 1),
                (2, 2),
            ]

            first, second = parts[coding]
            await until(lambda: _is(porth, message_id, 'sent'))
            await smsc.receipt(first.smsc_id)
            await asyncio.sleep(0.3)
            assert await porth.status(message_id) == 'sent'  # one part still out
            await smsc.receipt(second.smsc_id)
            await until(lambda: _is(porth, message_id, 'delivered'))
        await until(lambda: len(app.requests) == 2)
        await asyncio.sleep(0.5)  # time enough for a callback per part

    # REST has no coding field: porth chose UCS-2, and the text survives it
    in_order = sorted(
        parts[0x08], key=lambda s: s.pdu.get_concatenated_info().part_number
    )
    content = (s.pdu.get_message_content() for s in in_order)  # UDH stripped
    assert b''.join(content).decode('utf-16-be') == ucs2

    assert sorted(r.path for r in app.requests) == ['/status/0', '/status/8']
