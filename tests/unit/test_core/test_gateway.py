"""Unit tests for SMSGateway listener lifecycle."""

import socket

import pytest

from porth.config.settings import HTTPConfig, KannelConfig, Settings
from porth.main import SMSGateway


@pytest.mark.asyncio
async def test_failed_bind_leaves_runner_for_stop_to_clean_up():
    with socket.socket() as taken:
        taken.bind(('127.0.0.1', 0))
        taken.listen()
        port = taken.getsockname()[1]
        gateway = SMSGateway(
            Settings(
                http=HTTPConfig(host='127.0.0.1', port=0),
                kannel=KannelConfig(host='127.0.0.1', port=port),
            )
        )
        with pytest.raises(OSError):
            await gateway.start()
        # the Kannel runner is tracked despite its failed bind
        assert len(gateway.servers) == 2
        await gateway.stop()
    assert all(runner.server is None for runner in gateway.servers)
