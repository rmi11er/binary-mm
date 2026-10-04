import asyncio

import httpx
import pytest

from bmm.scan.http import GetOnlyClient
from bmm.scan.models import VenueNotPublic, VenueRateLimited

URL = "https://api.example.com/x"


def client(handler, **kw):
    return GetOnlyClient(
        {"api.example.com"},
        min_interval_s=0,
        backoff_s=0,
        transport=httpx.MockTransport(handler),
        **kw,
    )


def test_429_is_retried_then_succeeds():
    seen = []

    def h(req):
        seen.append(1)
        return httpx.Response(429 if len(seen) < 3 else 200, json={"ok": 1})

    assert asyncio.run(client(h).get_json(URL)) == {"ok": 1} and len(seen) == 3


def test_persistent_429_is_skippable_not_a_crash():
    with pytest.raises(VenueRateLimited):
        asyncio.run(client(lambda r: httpx.Response(429)).get_json(URL))
    assert issubclass(VenueRateLimited, VenueNotPublic)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_walls_are_not_public(status):
    with pytest.raises(VenueNotPublic):
        asyncio.run(client(lambda r: httpx.Response(status)).get_json(URL))


def test_non_json_page_is_not_public():
    with pytest.raises(VenueNotPublic):
        asyncio.run(client(lambda r: httpx.Response(200, text="<html>")).get_json(URL))


def test_only_get_is_ever_sent():
    methods = []

    def h(req):
        methods.append(req.method)
        return httpx.Response(200, json={})

    asyncio.run(client(h).get_json(URL, {"a": "1"}))
    assert methods == ["GET"]
