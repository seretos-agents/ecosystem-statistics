"""CLI entry point: `python -m ecosystem_statistics collect --since --until --out
[--cache-dir]` (plan #9)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .collect import run_collect


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecosystem_statistics")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="Collect branch_churn and main_rework for a UTC date range."
    )
    collect_parser.add_argument("--since", required=True, type=_parse_date)
    collect_parser.add_argument("--until", required=True, type=_parse_date)
    collect_parser.add_argument("--out", required=True, type=Path)
    collect_parser.add_argument("--cache-dir", default=Path(".cache/repos"), type=Path)
    collect_parser.add_argument("--config-dir", default=Path("config"), type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "collect":
        run_collect(
            since=args.since,
            until=args.until,
            out_dir=args.out,
            cache_dir=args.cache_dir,
            config_dir=args.config_dir,
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
