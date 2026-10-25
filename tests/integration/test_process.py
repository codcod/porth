"""The real bin/porth process (POR-054): SIGTERM, the database lock, and json logs.

Every other black-box test builds porth in-process; this one starts bin/porth as an
operator does, signals it, and reads what it prints.
"""

import asyncio
import contextlib
import json
import pathlib
import signal
import subprocess
import sys

import aiohttp
import pytest

from tests.integration.conftest import DSN
from tests.integration.test_features import ROUTE_ALL, SMSC, clean  # noqa: F401 (a fixture)
from tests.integration.test_repository import pytestmark  # noqa: F401
from tests.integration.test_restart import until
from tests.integration.test_smpp_flow import free_port

ROOT = pathlib.Path(__file__).parents[2]
TEXTS = ['First of three', 'Second of three', 'Third of three']


class Process:
    """bin/porth on a config file, its stdout and stderr in one log file per run."""

    def __init__(self, tmp_path: pathlib.Path, config: str) -> None:
        self.tmp_path = tmp_path
        self.http_port = free_port()
        self.config = tmp_path / 'config.toml'
        self.config.write_text(
            f'[porth]\ndb = "{DSN}"\nlog_level = "INFO"\nlog_format = "json"\n'
            f'[porth.http]\nhost = "127.0.0.1"\nport = {self.http_port}\n'
            f'[porth.kannel]\nhost = "127.0.0.1"\nport = {free_port()}\n' + config
        )
        self.logs: list[pathlib.Path] = []
        self.proc: subprocess.Popen | None = None

    @property
    def rest(self) -> str:
        return f'http://127.0.0.1:{self.http_port}'

    async def start(self) -> None:
        log = self.tmp_path / f'run-{len(self.logs)}.log'
        self.logs.append(log)
        with log.open('wb') as out:  # files, not pipes: a full pipe would block porth
            self.proc = subprocess.Popen(
                [sys.executable, 'bin/porth', str(self.config)],
                cwd=ROOT,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        try:
            await until(self.healthy)
        except TimeoutError:
            raise AssertionError(f'porth never got healthy:\n{self.output()}') from None

    async def healthy(self) -> bool:
        assert self.proc is not None and self.proc.poll() is None, self.output()
        with contextlib.suppress(aiohttp.ClientError):
            async with aiohttp.ClientSession() as session:
                async with session.get(f'{self.rest}/health') as r:
                    return r.status == 200
        return False

    async def terminate(self) -> int:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        # Off the loop: the SMSC on this loop must answer porth's unbind
        return await asyncio.to_thread(self.proc.wait, 15)

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def output(self) -> str:
        return ''.join(log.read_text() for log in self.logs)

    async def send(self, text: str) -> str:
        body = {'source_addr': '1234', 'destination_addr': '306900000001'}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{self.rest}/api/v1/sms/send', json=body | {'message_text': text}
            ) as r:
                assert r.status == 200, await r.text()
                return (await r.json())['message_id']


@pytest.mark.asyncio
@pytest.mark.usefixtures('clean')
async def test_sigterm_exits_cleanly_and_a_restart_sends_each_queued_message_once(
    tmp_path,
):
    smsc = SMSC('op')  # not started: the first run can only queue
    porth = Process(
        tmp_path, smsc.toml() + ROUTE_ALL + '[porth.delivery]\nretry_delay = 60\n'
    )
    try:
        await porth.start()
        ids = [await porth.send(text) for text in TEXTS]
        assert await porth.terminate() == 0, porth.output()

        await smsc.start()
        # It starts at all only if the first run released the database lock
        await porth.start()
        await until(lambda: sorted(smsc.texts()) == sorted(TEXTS), timeout=15)
        await asyncio.sleep(2)  # time enough for a wrongful resend
        assert await porth.terminate() == 0, porth.output()
    finally:
        porth.kill()
        await smsc.stop()

    assert sorted(smsc.texts()) == sorted(TEXTS)

    lines = [line for line in porth.output().splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]  # every line is JSON
    assert all({'level', 'logger', 'message'} <= record.keys() for record in records)
    assert set(ids) <= {record.get('message_id') for record in records}
