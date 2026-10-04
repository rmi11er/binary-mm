from datetime import date

import pytest

from bmm.clock import ct_to_ns
from bmm.spotcheck.contracts import (
    PRODUCTS,
    OptExpiry,
    expiry_ts_ns,
    lead_month,
    load_products,
    match_expiry,
    parse_gc_month,
)

QTR = [date(2026, 12, 18), date(2027, 3, 19), date(2027, 6, 18)]


def test_lead_month_normal_day():
    assert lead_month(date(2026, 10, 5), QTR) == date(2026, 12, 18)


def test_lead_month_rolls_on_final_settlement_day():
    assert lead_month(date(2026, 12, 18), QTR) == date(2027, 3, 19)


def test_parse_gc_month():
    assert parse_gc_month("Gold Futures Dec 2026 settle above 2650?") == "202612"
    assert parse_gc_month("COMEX GC FEB27") == "202702"
    assert parse_gc_month("Gold price question") is None


FIX = ct_to_ns(date(2026, 10, 5), 15, 0)


def ox(ns, con=111):
    return OptExpiry(ns, con, "E1A", "20261005")


def test_exact_match():
    m = match_expiry(FIX, 111, [ox(FIX + 30_000_000_000), ox(FIX + 86_400_000_000_000)])
    assert m and not m.mismatch and [r for _, r in m.chosen] == ["match"]


def test_bracketing_mismatch():
    m = match_expiry(FIX, 111, [ox(FIX - 3_600_000_000_000), ox(FIX + 7_200_000_000_000)])
    assert m and m.mismatch and [r for _, r in m.chosen] == ["below", "above"]


def test_one_sided_bracket():
    m = match_expiry(FIX, 111, [ox(FIX + 7_200_000_000_000)])
    assert m and m.mismatch and [r for _, r in m.chosen] == ["above"]


def test_ignores_other_underlyings():
    assert match_expiry(FIX, 111, [ox(FIX, con=222)]) is None


def test_products_fix_times():
    assert PRODUCTS["forecastex-spx"].fix_ct == (15, 0) and PRODUCTS["cme-gold"].fix_ct == (12, 30)
    assert set(PRODUCTS) == {"forecastex-spx", "forecastex-ndx", "forecastex-gold", "cme-gold"}


def test_expiry_ts_ns_uses_tz():
    assert expiry_ts_ns("20261005", "1500", "US/Central") == FIX


def test_example_toml_matches_defaults():
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    assert load_products(root / "config/spotcheck.example.toml") == PRODUCTS


def test_load_products_override(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[products.cme-gold]\nib_symbol = "XYZ"\nfix_ct = [12, 31]\n')
    out = load_products(p)
    assert out["cme-gold"].ib_symbol == "XYZ" and out["cme-gold"].fix_ct == (12, 31)
    assert out["forecastex-spx"] == PRODUCTS["forecastex-spx"]
    p.write_text("[products.nope]\nx = 1\n")
    with pytest.raises(ValueError):
        load_products(p)
