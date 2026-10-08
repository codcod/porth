"""Restart recovery against a real PostgreSQL and an smppai SMSC (make db-up migrate)."""

import asyncio
import inspect
import uuid

import aiohttp
import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiohttp import web
from aiohttp.test_utils import TestServer
from monobase.db import make_engine
from smpp import SMPPServer

from porth.adapters.tables import dlr_callbacks, messages
from porth.config.settings import (
    DeliveryConfig,
    HTTPConfig,
    KannelConfig,
    RoutingConfig,
    Settings,
    SMPPClientConfig,
)
from porth.entrypoints.app import Gateway
from tests.integration.conftest import DSN
from tests.integration.test_repository import pytestmark  # noqa: F401
from tests.integration.test_smpp_flow import free_port


class DLRTarget:
    """A dlr-url server answering `status`; records each request's query."""

    def __init__(self) -> None:
        self.queries: list[dict[str, str]] = []
        self.status = 200
        app = web.Application()
        app.router.add_get('/dlr', self.handle)
        self.server = TestServer(app)

    async def handle(self, request: web.Request) -> web.Response:
        self.queries.append(dict(request.query))
        return web.Response(status=self.status)

    def url(self) -> str:
        return str(self.server.make_url('/dlr')) + '?d=%d&f=%F'


class SMSC:
    """An smppai SMSC answering each submit_sm with this test's own SMSC id."""

    def __init__(self, text: str) -> None:
        self.port = free_port()
        self.smsc_id = f'smsc-{uuid.uuid4()}'
        self.text = text
        self.submits = 0
        self.server: SMPPServer | None = None

    async def start(self) -> None:
        self.server = SMPPServer(
            host='127.0.0.1', port=self.port, setup_signal_handlers=False
        )
        self.server.on_message_received = self.on_submit
        await self.server.start()

    def on_submit(self, server, session, pdu) -> str:
        # The database may hold other runs' unsent rows; count only this test's
        if pdu.get_message_text() == self.text:
            self.submits += 1
        return self.smsc_id

    async def receipt(self) -> None:
        assert self.server is not None
        assert await self.server.deliver_sm(
            'porth',
            source_addr='5678',
            destination_addr='1234',
            short_message=(
                f'id:{self.smsc_id} sub:001 dlvrd:001 submit date:2609261200 '
                'done date:2609261201 stat:DELIVRD err:000 text:'
            ),
            esm_class=0x04,
        )

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()


@pytest_asyncio.fixture
async def env(database):
    """(engine, ids, target): the messages in ids are deleted afterwards."""
    engine = make_engine(DSN)
    ids: list[str] = []
    target = DLRTarget()
    await target.server.start_server()
    yield engine, ids, target
    await target.server.close()
    async with engine.begin() as conn:
        await conn.execute(sa.delete(messages).where(messages.c.message_id.in_(ids)))
    await engine.dispose()


def gateway(smsc: SMSC, kannel_port: int, name: str = 'a') -> Gateway:
    """A gateway whose one SMSC, named name, takes every number."""
    return Gateway(
        Settings(
            db=DSN,
            http=HTTPConfig(host='127.0.0.1', port=0),
            kannel=KannelConfig(host='127.0.0.1', port=kannel_port),
            smsc={
                name: SMPPClientConfig(
                    host='127.0.0.1',
                    port=smsc.port,
                    system_id='porth',
                    password='pw',
                )
            },
            routing=RoutingConfig(default=name),
            delivery=DeliveryConfig(retry_delay=60, worker_count=1),
        )
    )


async def sendsms(kannel_port: int, text: str, dlr_url: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f'http://127.0.0.1:{kannel_port}/cgi-bin/sendsms',
            params={
                'from': '1234',
                'to': '5678',
                'text': text,
                'dlr-url': dlr_url,
                'dlr-mask': '1',
            },
        ) as response:
            body = await response.text()
    assert body.startswith('0: Accepted for delivery'), body
    return body.split('Message-ID: ')[1]


async def status(engine, message_id: str) -> str:
    async with engine.connect() as conn:
        return await conn.scalar(
            sa.select(messages.c.status).where(messages.c.message_id == message_id)
        )


async def callback_stored(engine, message_id: str) -> bool:
    async with engine.connect() as conn:
        count = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(dlr_callbacks)
            .where(dlr_callbacks.c.message_id == message_id)
        )
    return count == 1


async def until(condition, timeout: float = 10) -> None:
    """Wait for condition(), which may return an awaitable."""
    async with asyncio.timeout(timeout):
        while True:
            result = condition()
            if await result if inspect.isawaitable(result) else result:
                return
            await asyncio.sleep(0.05)


async def send_through_restarts(
    env, smsc: SMSC, first_name: str = 'a', second_name: str = 'a'
) -> str:
    """Gateway 1 accepts a message while the SMSC is down and stops; gateway 2 sends
    it once the SMSC is up and stops. Each names the SMSC as given. Returns its id,
    left `sent`."""
    engine, ids, target = env
    kannel_port = free_port()

    first = gateway(smsc, kannel_port, first_name)
    await first.start()
    try:
        message_id = await sendsms(kannel_port, smsc.text, target.url())
        ids.append(message_id)
        # its first attempt fails (no SMSC), and the retry waits 60 s
        await until(lambda: first.engines[first_name]._retries)
    finally:
        await first.stop()
    assert await status(engine, message_id) == 'queued'

    await smsc.start()
    second = gateway(smsc, kannel_port, second_name)
    await second.start()
    try:
        await until(lambda: _is(engine, message_id, 'sent'))
    finally:
        await second.stop()
    assert smsc.submits == 1
    return message_id


async def _is(engine, message_id: str, expected: str) -> bool:
    return await status(engine, message_id) == expected


def bound(gw: Gateway) -> bool:
    return gw.smpp_clients[0].connected


@pytest.mark.asyncio
async def test_queued_message_survives_restart_and_is_sent_once(env):
    engine, _, target = env
    smsc = SMSC(f'restart {uuid.uuid4()}')
    try:
        message_id = await send_through_restarts(env, smsc)

        third = gateway(smsc, free_port())
        await third.start()
        try:
            await until(lambda: bound(third))
            await asyncio.sleep(0.5)  # time enough for a wrongful resend
            assert smsc.submits == 1

            await smsc.receipt()
            await until(lambda: _is(engine, message_id, 'delivered'))
            await until(lambda: target.queries)
        finally:
            await third.stop()
    finally:
        await smsc.stop()
    assert target.queries == [{'d': '1', 'f': smsc.smsc_id}]
    assert not await callback_stored(engine, message_id)


@pytest.mark.asyncio
async def test_dlr_url_call_cut_off_by_a_stop_is_made_after_restart(env):
    engine, _, target = env
    smsc = SMSC(f'callback {uuid.uuid4()}')
    try:
        message_id = await send_through_restarts(env, smsc)

        target.status = 500  # the client's server is down until this gateway stops
        third = gateway(smsc, free_port())
        await third.start()
        try:
            await until(lambda: bound(third))
            await smsc.receipt()
            await until(lambda: target.queries)
        finally:
            await third.stop()
        assert await status(engine, message_id) == 'delivered'
        assert await callback_stored(engine, message_id)

        target.status = 200
        fourth = gateway(smsc, free_port())
        await fourth.start()
        try:
            await until(lambda: len(target.queries) == 2)
            await until(lambda: _not(callback_stored(engine, message_id)))
        finally:
            await fourth.stop()
    finally:
        await smsc.stop()
    assert len(target.queries) == 2
    assert all(q == {'d': '1', 'f': smsc.smsc_id} for q in target.queries)


async def _not(awaitable) -> bool:
    return not await awaitable


@pytest.mark.asyncio
async def test_message_whose_smsc_was_removed_is_rerouted_on_restart(env):
    engine, _, _ = env
    smsc = SMSC(f'reroute {uuid.uuid4()}')
    try:
        message_id = await send_through_restarts(env, smsc, 'old', 'new')
    finally:
        await smsc.stop()
    async with engine.connect() as conn:
        stored = await conn.scalar(
            sa.select(messages.c.smsc).where(messages.c.message_id == message_id)
        )
    assert stored == 'new'
