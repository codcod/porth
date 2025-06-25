"""Pytest configuration and fixtures."""

import asyncio
import pytest
from typing import AsyncGenerator

from porth.config.settings import Settings
from porth.core.queue import MessageQueue


@pytest.fixture(scope='session')
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def test_settings() -> Settings:
    """Test settings fixture."""
    return Settings.load_from_file('config/test.yml')


@pytest.fixture
async def message_queue() -> AsyncGenerator[MessageQueue, None]:
    """Message queue fixture."""
    queue = MessageQueue(maxsize=100)
    yield queue
