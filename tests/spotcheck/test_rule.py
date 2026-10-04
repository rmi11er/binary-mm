import polars as pl

from bmm.spotcheck import rule
from bmm.spotcheck.rule import Verdict, verdict


def test_constants_pinned():
    """Owner-owned (docs/spotcheck_spec.md 'Decision rule'). Never edit these to make a test pass."""
    assert rule.GO_MEDIAN_ABS_GAP == 0.03
    assert rule.GO_PCT_GT_3C == 0.30
    assert rule.GO_MIN_PERSISTENCE == 3
    assert rule.GO_MIN_MEDIAN_SIZE == 20
    assert rule.KILL_MEDIAN_ABS_GAP == 0.02
    assert rule.KILL_PCT_TAKER_LE_0 == 0.90
    assert rule.VERDICT_FEE == 0.02


def gaps(rows, p="x"):
    """rows: bucket -> (median_abs_gap, pct_gt_3c)."""
    return pl.DataFrame(
        [
            {
                "product": p,
                "m_bucket": b,
                "n": 50,
                "median_abs_gap": m,
                "iqr": 0.01,
                "pct_gt_2c": min(1.0, pct + 0.1),
                "pct_gt_3c": pct,
            }
            for b, (m, pct) in rows.items()
        ],
        schema={
            "product": pl.String,
            "m_bucket": pl.String,
            "n": pl.Int64,
            "median_abs_gap": pl.Float64,
            "iqr": pl.Float64,
            "pct_gt_2c": pl.Float64,
            "pct_gt_3c": pl.Float64,
        },
    )


def pers(rows, scope="matched", p="x"):
    return pl.DataFrame(
        [
            {
                "product": p,
                "m_bucket": b,
                "n_runs": 5,
                "median_run": r,
                "max_run": int(r) + 2,
                "scope": scope,
            }
            for b, r in rows.items()
        ],
        schema={
            "product": pl.String,
            "m_bucket": pl.String,
            "n_runs": pl.Int64,
            "median_run": pl.Float64,
            "max_run": pl.Int64,
            "scope": pl.String,
        },
    )


def sizes(rows, scope="matched", p="x"):
    return pl.DataFrame(
        [
            {"product": p, "m_bucket": b, "median_size": s, "n": 20, "scope": scope}
            for b, s in rows.items()
        ],
        schema={
            "product": pl.String,
            "m_bucket": pl.String,
            "median_size": pl.Float64,
            "n": pl.Int64,
            "scope": pl.String,
        },
    )


def taker(le0, fee=0.02, p="x"):
    return pl.DataFrame(
        [
            {
                "product": p,
                "fee": fee,
                "n_snapshots": 60,
                "pct_snapshots_best_edge_gt_1c": 0.5,
                "pct_snapshots_best_edge_gt_2c": 0.3,
                "pct_snapshots_best_edge_le_0": le0,
                "capturable_median": 10.0,
                "capturable_p90": 20.0,
            }
        ],
        schema={
            "product": pl.String,
            "fee": pl.Float64,
            "n_snapshots": pl.Int64,
            "pct_snapshots_best_edge_gt_1c": pl.Float64,
            "pct_snapshots_best_edge_gt_2c": pl.Float64,
            "pct_snapshots_best_edge_le_0": pl.Float64,
            "capturable_median": pl.Float64,
            "capturable_p90": pl.Float64,
        },
    )


GO_GAPS = {"<0.5": (0.01, 0.0), "1-2": (0.044, 0.9)}


def run(g_all=None, g_m=None, per=None, siz=None, tk=None):
    g_all = GO_GAPS if g_all is None else g_all
    g_m = g_all if g_m is None else g_m
    per = {"1-2": 11.0} if per is None else per
    siz = {"1-2": 40.0} if siz is None else siz
    tk = 0.03 if tk is None else tk
    return verdict(
        "x",
        gaps(g_all),
        gaps(g_m),
        pl.concat([pers(per, "matched"), pers(per, "all")]),
        pl.concat([sizes(siz, "matched"), sizes(siz, "all")]),
        taker(tk),
    )


def test_go():
    v = run()
    assert isinstance(v, Verdict) and v.label == "GO"
    assert v.numbers["matched.1-2.median_abs_gap"] == 0.044
    assert v.numbers["matched.1-2.median_run"] == 11.0
    assert v.numbers["matched.1-2.median_size"] == 40.0
    assert v.numbers["pct_snapshots_best_edge_le_0"] == 0.03


def test_go_thresholds_are_inclusive():
    v = run(g_all={"1-2": (0.03, 0.30)}, per={"1-2": 3.0}, siz={"1-2": 20.0})
    assert v.label == "GO"


def test_kill_when_all_buckets_small_gap():
    v = run(g_all={"<0.5": (0.005, 0.0), "1-2": (0.0199, 0.0)}, tk=0.5)
    assert v.label == "KILL"
    assert any("median |gap|" in r for r in v.reasons)


def test_kill_when_taker_edge_rarely_positive():
    v = run(g_all={"1-2": (0.025, 0.1)}, tk=0.91)
    assert v.label == "KILL"
    assert any("taker" in r for r in v.reasons)


def test_taker_clause_is_strictly_greater_than_90pct():
    assert run(g_all={"1-2": (0.025, 0.1)}, tk=0.90).label == "INVESTIGATE"


def test_kill_precedes_go():
    assert run(tk=0.95).label == "KILL"


def test_kill_uses_verdict_fee_row():
    v = verdict(
        "x",
        gaps(GO_GAPS),
        gaps(GO_GAPS),
        pl.concat([pers({"1-2": 5.0}), pers({"1-2": 5.0}, "all")]),
        pl.concat([sizes({"1-2": 40.0}), sizes({"1-2": 40.0}, "all")]),
        pl.concat([taker(0.99, fee=0.01), taker(0.10, fee=0.02)]),
    )
    assert v.label == "GO"


def test_go_only_with_mismatch_is_investigate():
    v = run(g_all=GO_GAPS, g_m={"1-2": (0.02, 0.1)})
    assert v.label == "INVESTIGATE"
    assert any("mismatch" in r for r in v.reasons)


def test_no_matched_data_is_investigate():
    v = verdict(
        "x",
        gaps(GO_GAPS),
        gaps({}),
        pers({"1-2": 5.0}, "all"),
        sizes({"1-2": 40.0}, "all"),
        taker(0.1),
    )
    assert v.label == "INVESTIGATE"
    assert any("mismatch" in r for r in v.reasons)


def test_tiny_size_is_investigate():
    v = run(siz={"1-2": 3.0})
    assert v.label == "INVESTIGATE"
    assert any("size" in r for r in v.reasons)


def test_no_persistence_is_investigate():
    v = run(per={"1-2": 2.0})
    assert v.label == "INVESTIGATE"
    assert any("persist" in r for r in v.reasons)


def test_moderate_gaps_investigate():
    v = run(g_all={"1-2": (0.025, 0.2)}, tk=0.3)
    assert v.label == "INVESTIGATE"


def test_no_data_is_not_kill():
    v = verdict("x", gaps({}), gaps({}), pers({}), sizes({}), taker(0.99))
    assert v.label == "INVESTIGATE"
    assert any("no data" in r for r in v.reasons)
