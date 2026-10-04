import json
from pathlib import Path

import pytest

from bmm.scan.models import VenueNotPublic

FIX = Path(__file__).resolve().parents[1] / "fixtures/scan"

ROUTES = {  # (substring of url, fixture) in match order
    "pm_us": [
        ("/book", "pm_us/book_active.json"),
        ("/incentives", "pm_us/incentives.json"),
    ],
    "kalshi": [
        ("/orderbook", "kalshi/orderbook.json"),
        ("/markets/trades", "kalshi/trades.json"),
        ("/incentive_programs", "kalshi/incentive_programs.json"),
        ("/markets/", "kalshi/market.json"),
    ],
    "gemini": [
        ("/liquidity-rewards/config", "gemini/liquidity_rewards_config.json"),
        ("/liquidity-rewards/events", "gemini/liquidity_rewards_events.json"),
        ("/prediction-markets/events/", "gemini/event.json"),
        ("/v1/book/", "gemini/book.json"),
        ("/v1/trades/", "gemini/trades.json"),
    ],
}


class FakeClient:
    def __init__(self, venue, status=200):
        self.venue, self.status, self.calls = venue, status, []

    async def get_json(self, url, params=None):
        self.calls.append((url, params))
        if self.status in (401, 403):
            raise VenueNotPublic(f"HTTP {self.status}")
        for sub, fx in ROUTES[self.venue]:
            if sub in url:
                return json.loads((FIX / fx).read_text())
        raise AssertionError(f"unrouted url {url}")


@pytest.fixture
def fake_client():
    return FakeClient
