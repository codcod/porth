"""Async helper utilities."""

import asyncio
import logging
from typing import Any, Callable, Optional, List
from functools import wraps

logger = logging.getLogger(__name__)


def async_retry(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Decorator for async functions with retry logic."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        wait_time = delay * (backoff**attempt)
                        logger.warning(
                            f'Attempt {attempt + 1} failed, retrying in {wait_time}s: {e}'
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f'All {max_retries + 1} attempts failed: {e}')

            if last_exception:
                raise last_exception

        return wrapper

    return decorator


async def gather_with_semaphore(
    semaphore: asyncio.Semaphore, tasks: List[Callable]
) -> List[Any]:
    """Execute tasks with semaphore limiting concurrency."""

    async def limited_task(task):
        async with semaphore:
            return await task()

    return await asyncio.gather(*[limited_task(task) for task in tasks])


class AsyncTimer:
    """Async timer for periodic tasks."""

    def __init__(self, interval: float, callback: Callable, *args, **kwargs):
        self.interval = interval
        self.callback = callback
        self.args = args
        self.kwargs = kwargs
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def start(self) -> None:
        """Start the timer."""
        if self.running:
            return

        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the timer."""
        if not self.running:
            return

        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Internal timer loop."""
        while self.running:
            try:
                await asyncio.sleep(self.interval)
                if self.running:  # Check again after sleep
                    await self.callback(*self.args, **self.kwargs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f'Error in async timer callback: {e}')


async def timeout_after(seconds: float, coro) -> Any:
    """Execute coroutine with timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        logger.warning(f'Operation timed out after {seconds} seconds')
        raise
