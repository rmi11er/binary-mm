"""`bmm scan rewards|report`. Read-only; refuses to run in live mode."""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str]) -> int:
    if os.environ.get("BMM_MODE") == "live":
        print("bmm scan is read-only and refuses to run with BMM_MODE=live", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="bmm scan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rewards")
    r.add_argument("--once", action="store_true")
    r.add_argument("--interval", type=int, default=300)
    r.add_argument("--venues", default="pm_us,kalshi,gemini")
    r.add_argument("--full", action="store_true", help="force the hourly (full) tier")
    r.add_argument("--top", type=int, default=40, help="markets per fast-tier scan")
    rep = sub.add_parser("report")
    rep.add_argument("--date", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "rewards":
        from bmm.scan.runner import run

        return run(args.venues.split(","), args.once, args.interval, args.full, args.top)
    from bmm.scan.report import write_report

    return write_report(args.date)
