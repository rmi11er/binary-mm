"""`bmm spotcheck collect|report`. Market data only; there is no --live flag.

Safety checks (BMM_MODE, port, host) run before anything is built, and `ibsource` is imported
lazily so that a refused invocation never loads the IB library surface.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

from bmm.clock import now_ns, ns_to_ct
from bmm.spotcheck.contracts import PRODUCTS, ProductSpec, load_products
from bmm.spotcheck.safety import SafetyError, check_host, check_mode, check_port

DEFAULT_CONFIG = "config/spotcheck.toml"
ALL_PRODUCTS = ",".join(PRODUCTS)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bmm spotcheck",
        description="Market-data-only check of event-contract binaries against the options "
        "surface. Connects to IB Gateway paper (port 4002) read-only.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="take snapshots around each product's fix time")
    c.add_argument("--product", default=ALL_PRODUCTS, help=f"comma list of: {ALL_PRODUCTS}")
    c.add_argument("--question-date", help="YYYY-MM-DD (default: today, CT)")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                   help="explicitly allowlist one non-loopback host (repeatable)")  # fmt: skip
    c.add_argument("--port", type=int, default=4002)
    c.add_argument("--client-id", type=int, default=31)
    c.add_argument("--out", default="data/spotcheck")
    c.add_argument("--config", default=DEFAULT_CONFIG)
    c.add_argument("--gc-month", help="YYYYMM of the gold future when METLG's name can't be parsed")
    c.add_argument("--fixture", help="synthetic session JSON; virtual clock, no network")
    r = sub.add_parser("report", help="turn collected sessions into a verdict report")
    r.add_argument("--from", dest="d_from", required=True)
    r.add_argument("--to", dest="d_to", required=True)
    r.add_argument("--data", default="data/spotcheck")
    r.add_argument("--out", default="reports")
    r.add_argument("--name")
    return ap


class _VirtualClock:
    def __init__(self, start_ns: int) -> None:
        self.t = start_ns

    def __call__(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += round(seconds * 1e9)


def _select(args: argparse.Namespace) -> list[ProductSpec]:
    cfg = Path(args.config)
    if not cfg.exists() and args.config != DEFAULT_CONFIG:
        raise ValueError(f"config not found: {cfg}")
    table = load_products(cfg if cfg.exists() else None)
    keys = [k.strip() for k in args.product.split(",") if k.strip()]
    unknown = [k for k in keys if k not in table]
    if unknown or not keys:
        raise ValueError(f"unknown product(s) {unknown}; choose from {sorted(table)}")
    return [table[k] for k in keys]


async def _run(args: argparse.Namespace, specs: list[ProductSpec]) -> dict[str, str]:
    from bmm.spotcheck.collect import collect
    from bmm.spotcheck.schema import ParquetSink

    sink = ParquetSink(Path(args.out))
    if args.fixture:
        from bmm.spotcheck.fixture import FixtureSource

        src = FixtureSource.from_json(args.fixture)
        qd = (
            date.fromisoformat(args.question_date)
            if args.question_date
            else date.fromisoformat(src.session.question_date)
        )
        firsts = [src.session.products[s.key].snaps[0].ts_ns for s in specs
                  if s.key in src.session.products]  # fmt: skip
        vc = _VirtualClock(min(firsts) if firsts else now_ns())
        src.set_clock(vc)
        return await collect(src, specs, qd, sink, vc, vc.sleep, args.gc_month)
    qd = date.fromisoformat(args.question_date) if args.question_date else ns_to_ct(now_ns()).date()
    from bmm.spotcheck.ibsource import IBSource  # only after every safety check has passed

    live = await IBSource.connect(args.host, args.port, args.client_id, allow_hosts=args.allow_host)
    try:
        return await collect(live, specs, qd, sink, now_ns, asyncio.sleep, args.gc_month)
    finally:
        await live.close()


def _collect(args: argparse.Namespace) -> int:
    try:
        check_mode(os.environ)
        check_port(args.port)
        check_host(args.host, args.allow_host)
    except SafetyError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    try:
        specs = _select(args)
        status = asyncio.run(_run(args, specs))
    except SafetyError as e:  # e.g. non-paper account detected after connecting
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    except (ValueError, OSError, TimeoutError, ConnectionError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    for k, v in status.items():
        print(f"{k}: {v}")
    return 0 if any(v == "ok" for v in status.values()) else 2


def _report(args: argparse.Namespace) -> int:
    from bmm.spotcheck.report import write_report

    try:
        path = write_report(
            Path(args.data), date.fromisoformat(args.d_from), date.fromisoformat(args.d_to),
            Path(args.out), args.name,
        )  # fmt: skip
    except (ValueError, OSError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "collect":
        return _collect(args)
    return _report(args)
