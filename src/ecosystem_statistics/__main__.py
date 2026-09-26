"""CLI entry point: `python -m ecosystem_statistics collect --since --until --out
[--cache-dir]` (plan #9); `python -m ecosystem_statistics publish --worktree
[--since --until] [--remote --branch] [--cache-dir --config-dir]` (plan #10)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import publish
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

    publish_parser = subparsers.add_parser(
        "publish",
        help=(
            "Collect a UTC date range (default: yesterday) and publish it to the "
            "`data` branch, committing only on a real diff."
        ),
    )
    publish_parser.add_argument("--since", type=_parse_date, default=None)
    publish_parser.add_argument("--until", type=_parse_date, default=None)
    publish_parser.add_argument("--remote", default="origin")
    publish_parser.add_argument("--branch", default="data")
    publish_parser.add_argument("--worktree", required=True, type=Path)
    publish_parser.add_argument("--cache-dir", default=Path(".cache/repos"), type=Path)
    publish_parser.add_argument("--config-dir", default=Path("config"), type=Path)
    # Stashed on the namespace so main() can raise a `publish`-specific usage/error
    # message (naming --since/--until) instead of the top-level parser's generic one.
    publish_parser.set_defaults(_error=publish_parser.error)

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

    if args.command == "publish":
        if (args.since is None) != (args.until is None):
            args._error("--since and --until must be given together")
        publish.publish_cli(
            repo_dir=Path.cwd(),
            remote=args.remote,
            branch=args.branch,
            worktree_dir=args.worktree,
            since=args.since,
            until=args.until,
            cache_dir=args.cache_dir,
            config_dir=args.config_dir,
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
