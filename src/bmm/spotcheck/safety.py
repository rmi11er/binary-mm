"""Hard guards for the market-data-only spotcheck (SPEC §3 rules 1-2)."""

from __future__ import annotations

from collections.abc import Mapping

ALLOWED_PORTS: frozenset[int] = frozenset({4002})  # IB Gateway paper. Never 4001.


class SafetyError(RuntimeError):
    pass


def check_port(port: int) -> int:
    if port not in ALLOWED_PORTS:
        raise SafetyError(f"spotcheck refuses port {port}; allowed: {sorted(ALLOWED_PORTS)}")
    return port


def check_mode(env: Mapping[str, str]) -> None:
    if env.get("BMM_MODE", "").lower() == "live":
        raise SafetyError("spotcheck is market-data only and refuses BMM_MODE=live")
