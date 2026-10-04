"""Hard guards for the market-data-only spotcheck (SPEC §3 rules 1-2)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

ALLOWED_PORTS: frozenset[int] = frozenset({4002})  # IB Gateway paper. Never 4001.


class SafetyError(RuntimeError):
    pass


LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})
ALLOWED_MODES: frozenset[str] = frozenset({"record", "paper", "sim"})
PAPER_ACCOUNT_PREFIX = "DU"  # [?] conventional IBKR paper prefix, see research/venue_facts.md


def check_port(port: int) -> int:
    if type(port) is not int or port not in ALLOWED_PORTS:
        raise SafetyError(f"spotcheck refuses port {port}; allowed: {sorted(ALLOWED_PORTS)}")
    return port


def check_mode(env: Mapping[str, str]) -> None:
    """Fail closed: BMM_MODE must be unset/blank or one of the known non-live modes."""
    mode = env.get("BMM_MODE", "").strip().casefold()
    if mode and mode not in ALLOWED_MODES:
        raise SafetyError(
            f"spotcheck is market-data only; refuses BMM_MODE={env.get('BMM_MODE')!r} "
            f"(allowed: unset or {sorted(ALLOWED_MODES)})"
        )


def check_host(host: str, allow: Iterable[str] = ()) -> str:
    """Loopback only, plus hosts the operator explicitly allowlisted (--allow-host)."""
    if host not in LOOPBACK_HOSTS and host not in set(allow):
        raise SafetyError(
            f"spotcheck refuses host {host!r}; loopback only unless --allow-host {host} is given"
        )
    return host


def check_accounts(accounts: Iterable[str]) -> list[str]:
    """Every managed account must look like a paper account. Fail closed on none."""
    accts = list(accounts)
    bad = [a for a in accts if not str(a).startswith(PAPER_ACCOUNT_PREFIX)]
    if not accts or bad:
        raise SafetyError(
            f"spotcheck refuses non-paper managed accounts {bad or accts!r} "
            f"(expected prefix {PAPER_ACCOUNT_PREFIX!r})"
        )
    return accts
