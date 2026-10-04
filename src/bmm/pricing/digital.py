"""Digital (cash-or-nothing, $1 payout) pricing on a futures underlying.

Conventions
-----------
F   : forward / futures price of the reference (e.g. ES for S&P-futures-referenced contracts)
K   : strike
T   : time to resolution in years (use the contract's *resolution timestamp*, not the options expiry)
sig : Black-76 implied vol at K (annualised)
df  : discount factor to payment date (~1 for intraday)

A YES contract on "F_T > K" is a digital call. NO = df - YES.
"""
from __future__ import annotations

import math
from collections.abc import Callable

from scipy.stats import norm

_EPS = 1e-12


def _d1_d2(F: float, K: float, T: float, sig: float) -> tuple[float, float]:
    if F <= 0 or K <= 0:
        raise ValueError("F and K must be positive")
    if T <= 0 or sig <= 0:
        raise ValueError("T and sig must be positive (handle expiry explicitly)")
    sd = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    return d1, d1 - sd


def black76_call(F: float, K: float, T: float, sig: float, df: float = 1.0) -> float:
    d1, d2 = _d1_d2(F, K, T, sig)
    return df * (F * norm.cdf(d1) - K * norm.cdf(d2))


def black76_vega(F: float, K: float, T: float, sig: float, df: float = 1.0) -> float:
    """dC/dsig (per 1.00 of vol, not per vol point)."""
    d1, _ = _d1_d2(F, K, T, sig)
    return df * F * norm.pdf(d1) * math.sqrt(T)


def digital_call(F: float, K: float, T: float, sig: float, df: float = 1.0) -> float:
    """Flat-vol digital: df * N(d2). Ignores skew -- use digital_call_smile for pricing."""
    _, d2 = _d1_d2(F, K, T, sig)
    return df * norm.cdf(d2)


def digital_call_smile(
    F: float,
    K: float,
    T: float,
    vol_fn: Callable[[float], float],
    df: float = 1.0,
    dk: float | None = None,
) -> float:
    """Skew-consistent digital = -dC/dK along the smile.

    = df*N(d2) - vega(K) * dsig/dK. With negative equity skew (dsig/dK < 0) the
    digital is worth MORE than the flat-vol price. Ignoring this is the classic
    retail mispricing in index binaries.
    """
    sig = vol_fn(K)
    h = dk if dk is not None else max(K * 1e-4, 1e-6)
    dsig_dk = (vol_fn(K + h) - vol_fn(K - h)) / (2 * h)
    return digital_call(F, K, T, sig, df) - black76_vega(F, K, T, sig, df) * dsig_dk


def digital_from_call_spread(c_low: float, c_high: float, k_low: float, k_high: float) -> float:
    """Model-free digital at the midpoint strike from a tight call spread.

    (C(K_lo) - C(K_hi)) / (K_hi - K_lo). Feed *mid* prices of liquid options that
    straddle the strike; this embeds skew automatically. Bias is second order in the
    strike width; keep the width small relative to sig*sqrt(T)*F.
    """
    if k_high <= k_low:
        raise ValueError("k_high must exceed k_low")
    return (c_low - c_high) / (k_high - k_low)


def digital_delta(F: float, K: float, T: float, sig: float, df: float = 1.0) -> float:
    """d(digital)/dF, sticky-strike, per 1 point of F per $1-payout contract."""
    _, d2 = _d1_d2(F, K, T, sig)
    return df * norm.pdf(d2) / (F * sig * math.sqrt(T))


def digital_gamma(F: float, K: float, T: float, sig: float, df: float = 1.0) -> float:
    """d2(digital)/dF2. Changes sign at d1=0; explodes near K as T->0 (pin risk)."""
    d1, d2 = _d1_d2(F, K, T, sig)
    return -df * norm.pdf(d2) * d1 / (F * F * sig * sig * T)


def interp_total_variance(t: float, t1: float, sig1: float, t2: float, sig2: float) -> float:
    """Vol at arbitrary time t via linear interpolation in total variance (sig^2 * t).

    Use when the binary resolves at a time that doesn't match a listed options expiry.
    For intraday horizons, upgrade to a trading-time clock (see SPEC section 6.3).
    """
    if not t1 < t2:
        raise ValueError("require t1 < t2")
    if t <= 0:
        raise ValueError("t must be positive")
    w1, w2 = sig1 * sig1 * t1, sig2 * sig2 * t2
    if w2 < w1:
        raise ValueError("calendar arbitrage: total variance decreasing")
    w = w1 + (w2 - w1) * (t - t1) / (t2 - t1)
    return math.sqrt(max(w, _EPS) / t)


def hedge_contracts(
    net_yes_contracts: float, delta_per_contract: float, point_value: float = 5.0
) -> float:
    """Futures contracts to SELL to flatten delta (positive => sell futures).

    net_yes_contracts: signed net YES exposure in $1-payout units; NO counts as negative YES.
    point_value: $ per index point per futures contract (MES=5, ES=50, MNQ=2, MGC=10).
    """
    return net_yes_contracts * delta_per_contract / point_value
