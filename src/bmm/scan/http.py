"""GET-only, host-allowlisted JSON client. No headers or credentials can be passed."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from bmm.scan.models import JSON, VenueNotPublic


class GetOnlyClient:
    def __init__(self, allow_hosts: set[str], timeout: float = 20.0) -> None:
        self._allow = set(allow_hosts)
        self._c = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> JSON:
        host = urlparse(url).hostname or ""
        if host not in self._allow:
            raise ValueError(f"host not allowlisted: {host!r}")
        r = await self._c.get(url, params=params)
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
