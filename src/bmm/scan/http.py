"""GET-only, host-allowlisted JSON client. No headers or credentials can be passed.

Requests are paced per host and retried with backoff on HTTP 429, so a full scan stays polite."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

from bmm.scan.models import JSON, VenueNotPublic, VenueRateLimited


class GetOnlyClient:
    def __init__(
        self,
        allow_hosts: set[str],
        timeout: float = 20.0,
        min_interval_s: float = 0.15,
        backoff_s: float = 1.0,
        retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow = set(allow_hosts)
        self._c = httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport)
        self._gap, self._backoff, self._retries = min_interval_s, backoff_s, retries
        self._last: dict[str, float] = {}

    async def _pace(self, host: str) -> None:
        wait = self._last.get(host, 0.0) + self._gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last[host] = time.monotonic()

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> JSON:
        host = urlparse(url).hostname or ""
        if host not in self._allow:
            raise ValueError(f"host not allowlisted: {host!r}")
        for attempt in range(self._retries + 1):
            await self._pace(host)
            r = await self._c.get(url, params=params)
            if r.status_code == 429 and attempt < self._retries:
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            break
        if r.status_code == 429:
            raise VenueRateLimited(f"{host}: HTTP 429 after {self._retries} retries")
        if r.status_code in (401, 403):
            raise VenueNotPublic(f"{host}: HTTP {r.status_code} (needs credentials)")
        if r.status_code >= 300:
            raise VenueNotPublic(f"{host}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise VenueNotPublic(f"{host}: response is not JSON") from e

    async def aclose(self) -> None:
        await self._c.aclose()
