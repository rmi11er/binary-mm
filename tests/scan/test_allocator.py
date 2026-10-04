import pytest

from bmm.scan.allocator import Half, allocate, cost_hr, ev, s_star, shrink_markout


def pr(R, D):
    return lambda s: R * s / (s + D) if s > 0 else 0.0


def H(**kw):
    d = {
        "key": "a:bid", "market": "a", "venue": "v", "two_sided": False,
        "reward": pr(10.0, 100.0), "c": 0.001, "coll": 0.5, "q": 1,
    }  # fmt: skip
    return Half(**(d | kw))


def test_cost_per_fill_floor_and_fees():
    # m=-0.03 adverse, rebate .01, fee .002 -> cost/fill .022; f = 50/100
    assert cost_hr(50.0, 100.0, -0.03, 0.002, 0.01, 0.6) == pytest.approx(
        0.5 * 0.022 + 0.6 * 0.05 / 8760, rel=1e-12
    )
    # favourable markout + big rebate -> cost/fill floored at 0
    assert cost_hr(50.0, 100.0, 0.05, 0.0, 0.02, 0.6) == pytest.approx(0.6 * 0.05 / 8760, rel=1e-12)


def test_shrink_markout():
    assert shrink_markout(-0.04, 25, -0.01) == (pytest.approx(-0.04), False)
    assert shrink_markout(-0.04, 5, -0.01) == (pytest.approx(0.25 * -0.04 + 0.75 * -0.01), False)
    assert shrink_markout(None, 0, -0.01) == (pytest.approx(-0.01), False)
    assert shrink_markout(None, 0, None) == (0.0, True)


def test_numeric_s_star_matches_bruteforce():
    h = H(reward=pr(10.0, 100.0), c=0.002)
    assert s_star(h, 5000) == max(range(5001), key=lambda s: ev(h, s))


def test_qualifying_raise_or_zero():
    assert s_star(H(reward=pr(10.0, 100.0), c=0.002, q=1000), 5000) == 1000  # EV(1000) > 0
    assert s_star(H(reward=pr(10.0, 100.0), c=0.05, q=1000), 5000) == 0  # EV(1000) < 0


def test_closed_form_used_and_rounded_to_best_integer():
    h = H(s_star_cf=123.4, reward=pr(10.0, 100.0), c=0.002)
    assert s_star(h, 5000) in (123, 124)


def test_caps_and_budget():
    hs = [
        H(
            key=f"m{i}:bid",
            market=f"m{i}",
            venue="v1" if i < 5 else "v2",
            reward=pr(1000.0, 10.0),
            c=1e-6,
            coll=1.0,
        )
        for i in range(10)
    ]
    out = {a.key: a for a in allocate(hs)}
    assert sum(a.collateral for a in out.values()) <= 8000 + 1e-9
    assert max(a.collateral for a in out.values()) <= 1200 + 1e-9
    for v in ("v1", "v2"):
        assert sum(out[h.key].collateral for h in hs if h.venue == v) <= 3200 + 1e-9


def test_greedy_matches_bruteforce_unit_blocks():
    a = H(key="a:bid", market="a", reward=pr(5.0, 20.0))
    b = H(key="b:bid", market="b", venue="w", reward=pr(8.0, 50.0))
    out = {x.key: x.s for x in allocate([a, b], budget=30, mkt_cap=1, venue_cap=1)}

    def tot(i, j):
        return ev(a, i) + ev(b, j)

    best = max(
        ((i, j) for i in range(61) for j in range(61) if 0.5 * (i + j) <= 30),
        key=lambda t: tot(*t),
    )
    assert tot(out["a:bid"], out["b:bid"]) == pytest.approx(tot(*best), abs=1e-9)


def test_two_sided_block_all_or_nothing():
    bid = H(key="m:bid", market="m", two_sided=True, reward=pr(10.0, 5.0), q=20)
    ask = H(key="m:ask", market="m", two_sided=True, reward=pr(0.01, 5.0), q=20)  # alone EV<0
    out = {x.key: x.s for x in allocate([bid, ask])}
    assert out["m:bid"] >= 20 and out["m:ask"] >= 20
    out = {x.key: x.s for x in allocate([bid, ask], budget=15)}  # block (40*0.5=20) doesn't fit
    assert out == {"m:bid": 0, "m:ask": 0}


def test_lower_bound_flag_passthrough():
    (a,) = allocate([H(c_lower_bound=True)])
    assert a.c_lower_bound
