"""Q-C1.4: realistic fetch latency (virtual clock advances per source call) and a future path
that makes ~60% of pre-fix ForecastEx snapshots non-simultaneous. The designed verdicts must hold:
spx stays GO and ndx stays KILL (the default zero-latency fixture is covered by test_report)."""

import asyncio
import re
from datetime import date

import polars as pl
import pytest

from bmm.clock import ct_to_ns
from bmm.spotcheck import metrics
from bmm.spotcheck.collect import collect
from bmm.spotcheck.contracts import PRODUCTS
from bmm.spotcheck.fixture import FixtureSource, make_latency_session
from bmm.spotcheck.report import write_report
from bmm.spotcheck.schema import ParquetSink

D = date(2026, 10, 5)
LATENCY_S = 2.0


class VClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += round(s * 1e9)

    def advance(self, s):
        self.t += round(s * 1e9)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("lat")
    vc = VClock(ct_to_ns(D, 11, 30))
    src = FixtureSource(make_latency_session(D), clock=vc, latency_s=LATENCY_S, advance=vc.advance)
    status = asyncio.run(
        collect(src, list(PRODUCTS.values()), D, ParquetSink(root / "d"), vc, vc.sleep)
    )
    assert set(status.values()) == {"ok"}, status
    md = write_report(root / "d", D, D, root / "r", name="lat")
    return root, md


def _verdicts(md):
    text = md.read_text().split("## Summary")[1]
    out = {}
    for ln in text.splitlines():
        m = re.match(r"- (\S+): \*\*(\w+)\*\* .*with non-simultaneous rows included: (\w+)", ln)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3))
    return out


def test_fetch_latency_is_visible_in_timestamps(run):
    root, _ = run
    t = metrics.load(root / "d", D, D)
    opt = t["options"].filter(pl.col("product") == "forecastex-spx")
    lag = (opt["opt_ts_ns"] - opt["ts_ns"]).min()
    assert lag >= round(2 * LATENCY_S * 1e9)  # at least a ladder, a future quote and chain calls
    fut = t["futures"].filter(pl.col("product") == "forecastex-spx")
    assert (fut["fut_ts_ns"] - fut["fut_before_ts_ns"]).max() > 0


def test_about_sixty_percent_of_forecastex_snapshots_are_non_simultaneous(run):
    root, _ = run
    ef = metrics.edge_frame(metrics.load(root / "d", D, D), 0.02)
    for p in ("forecastex-spx", "forecastex-ndx"):
        sh = metrics.sim_share(ef.filter(pl.col("product") == p))
        n, s = sh["snapshots"].sum(), sh["simultaneous"].sum()
        assert 0.25 <= s / n <= 0.55, (p, s, n)  # ~60% non-simultaneous (and a few late-fix ones)


def test_designed_verdicts_survive_latency(run):
    _, md = run
    v = _verdicts(md)
    assert v["forecastex-spx"][0] == "GO"
    assert v["forecastex-ndx"][0] == "KILL"
    assert v["forecastex-gold"][0] == "INVESTIGATE"
    assert v["cme-gold"][0] == "INVESTIGATE"
