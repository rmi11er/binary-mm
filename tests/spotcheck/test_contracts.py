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


QD = date(2026, 10, 5)


def test_parse_gc_month():
    assert parse_gc_month("Gold Futures Dec 2026 settle above 2650?", QD) == "202612"
    assert parse_gc_month("COMEX GC FEB27", QD) == "202702"
    assert parse_gc_month("Gold price question", QD) is None


def test_parse_gc_month_venue_format_and_false_positives():
    assert parse_gc_month("Will the DEC-26 gold futures settle above $4000 on Oct 15, 2026?", QD) == "202612"
    assert parse_gc_month("Will gold futures Dec-26 settle above $4000 on Oct 26, 2026?", QD) == "202612"
    assert parse_gc_month("Gold market 2026", QD) is None
    assert parse_gc_month("Mayor 25", QD) is None
    assert parse_gc_month("Decimal 2650", QD) is None
    assert parse_gc_month("on Oct 26, 2026", QD) is None  # day of month, not a year
    assert parse_gc_month("Gold Futures Dec 2019", QD) is None  # outside the year window
    assert parse_gc_month("December 2026 gold futures", QD) == "202612"


def test_parse_gc_month_ambiguous():
    assert parse_gc_month("Dec 2026 or Feb 2027 somewhere", QD) is None
    assert parse_gc_month("settle on Oct 2026 which is a very long way before the Feb-27 gold futures", QD) == "202702"


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


def test_expiry_ts_ns_formats_and_aliases():
    for hhmm in ("1500", "15:00", "15:00:00"):
        for tz in ("US/Central", "America/Chicago", "CST", "CST (Central Standard Time)", "CT"):
            assert expiry_ts_ns("20261005", hhmm, tz) == FIX
    with pytest.raises(ValueError):
        expiry_ts_ns("20261005", "1500", "Mars/Olympus")
    with pytest.raises(ValueError):
        expiry_ts_ns("20261005", "", "CST")


def test_match_tie_break_is_order_independent():
    a = OptExpiry(FIX, 111, "AAA", "20261005")
    b = OptExpiry(FIX, 111, "ZZZ", "20261005")
    assert match_expiry(FIX, 111, [a, b]).chosen[0][0].trading_class == "AAA"
    assert match_expiry(FIX, 111, [b, a]).chosen[0][0].trading_class == "AAA"


def test_bracket_sides_limited_to_two_days():
    far = ox(FIX + 3 * 86_400_000_000_000)
    assert match_expiry(FIX, 111, [far]) is None
    near = ox(FIX + 3_600_000_000_000)
    m = match_expiry(FIX, 111, [far, near])
    assert m and [r for _, r in m.chosen] == ["above"]


# --- quant-review C1: option class filter -------------------------------------------------------
def test_option_class_ok_always_excludes_event_contract_classes():
    from dataclasses import replace

    from bmm.spotcheck.contracts import option_class_ok

    cme = PRODUCTS["cme-gold"]
    assert not option_class_ok(cme, "ECGC") and not option_class_ok(cme, "ecgc")
    assert not option_class_ok(cme, "ECXX")
    assert option_class_ok(cme, "OG")
    assert not option_class_ok(PRODUCTS["forecastex-spx"], "EC1")
    assert option_class_ok(PRODUCTS["forecastex-spx"], "ES")
    only = replace(cme, opt_trading_classes=("OG",))
    assert option_class_ok(only, "OG") and not option_class_ok(only, "G2")
    # an allowlist cannot re-admit an excluded class
    assert not option_class_ok(replace(cme, opt_trading_classes=("ECGC",)), "ECGC")


def test_opt_trading_classes_from_toml(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text('[products.cme-gold]\nopt_trading_classes = ["OG", "G2"]\n')
    assert load_products(f)["cme-gold"].opt_trading_classes == ("OG", "G2")
    assert PRODUCTS["cme-gold"].opt_trading_classes == ()


def test_match_expiry_never_picks_event_class_after_filtering():
    fix = ct_to_ns(QD, 12, 30)
    decoy = OptExpiry(fix, 7, "ECGC", "20261005")
    real = OptExpiry(fix + 86_400 * 10**9, 7, "OG", "20261006")
    from bmm.spotcheck.contracts import option_class_ok

    kept = [e for e in (decoy, real) if option_class_ok(PRODUCTS["cme-gold"], e.trading_class)]
    m = match_expiry(fix, 7, kept)
    assert m and m.mismatch and m.chosen[0][0].trading_class == "OG"


def test_parse_month_without_anchor_and_strict_year():
    from bmm.spotcheck.contracts import parse_gc_month as p

    assert p("ES Dec-26 above 6500", QD, anchor=None, strict_year=True) == "202612"
    assert p("above 6500 on Oct 26", QD, anchor=None, strict_year=True) is None
    assert p("S&P Dec 2026 close", QD, anchor=None, strict_year=True) == "202612"


def test_allowlist_compare_is_case_insensitive():
    from dataclasses import replace

    from bmm.spotcheck.contracts import option_class_ok

    only = replace(PRODUCTS["cme-gold"], opt_trading_classes=("og",))
    assert option_class_ok(only, "OG") and option_class_ok(only, " og ")
    assert not option_class_ok(only, "G2")
