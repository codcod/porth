"""Outbound HTTP to client- and operator-supplied URLs: the Kannel dlr-url and REST
status callback (DLR), the MO URL. One-shot requests; callers own retries."""

import typing as tp

import aiohttp


class CallbackClient:
    """Owns one aiohttp session, opened by start() and closed by stop()."""

    def __init__(self, timeout: float):
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: tp.Optional[aiohttp.ClientSession] = None

    @property
    def started(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_status(self, url: str) -> int:
        """GET url, following redirects: the status. The body is never read."""
        assert self._session is not None
        async with self._session.get(url) as response:
            return response.status

    async def get(
        self, url: str, want_body: tp.Callable[[int, str], bool]
    ) -> tuple[int, str, tp.Optional[str]]:
        """GET url, following redirects: (status, content type, body text). The body
        is read, and strictly decoded, only when want_body(status, content type)."""
        assert self._session is not None
        async with self._session.get(url) as response:
            status, content_type = response.status, response.content_type
            text = await response.text() if want_body(status, content_type) else None
            return status, content_type, text

    async def post_json(self, url: str, body: dict[str, tp.Any]) -> int:
        """POST body as JSON to url, not following redirects: the status."""
        assert self._session is not None
        # Not followed: a 3xx would turn the POST into a body-less GET whose 2xx
        # counts as delivered, so the caller retries it like any non-2xx
        async with self._session.post(
            url, json=body, allow_redirects=False
        ) as response:
            return response.status
