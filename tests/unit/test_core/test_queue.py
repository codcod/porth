"""Unit tests for MessageQueue ordering: high priority first, FIFO within a level."""

import pytest

from porth.core.message import SMSMessage
from porth.core.queue import MessageQueue


def msg(text, priority='normal') -> SMSMessage:
    return SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text=text,
        protocol='http',
        priority=priority,
    )


async def take(queue) -> list[str]:
    return [(await queue.get()).message_text for _ in range(queue.qsize())]


@pytest.mark.asyncio
async def test_high_leaves_before_waiting_normals_which_keep_put_order():
    queue = MessageQueue()
    for item in (msg('n1'), msg('n2'), msg('n3'), msg('h', 'high')):
        await queue.put(item)
    assert await take(queue) == ['h', 'n1', 'n2', 'n3']


@pytest.mark.asyncio
async def test_two_highs_keep_put_order():
    queue = MessageQueue()
    for item in (msg('n'), msg('h1', 'high'), msg('h2', 'high')):
        await queue.put(item)
    assert await take(queue) == ['h1', 'h2', 'n']


@pytest.mark.asyncio
async def test_qsize_and_task_done():
    queue = MessageQueue()
    await queue.put(msg('a', 'high'))
    await queue.put(msg('b'))
    assert queue.qsize() == 2
    await queue.get()
    await queue.get()
    assert queue.empty()
    queue.task_done()
    queue.task_done()
    await queue.join()
