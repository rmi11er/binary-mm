"""bmm command line: a thin lazy dispatcher. Subcommand code lives with its package."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "spotcheck":
        from bmm.spotcheck.cli import main as spotcheck_main

        return spotcheck_main(args[1:])
    print("usage: bmm spotcheck {collect,report} ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
