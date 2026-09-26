"""Deterministic JSON writing (plan #9).

`json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False) + "\\n"`, written as raw
bytes (never a text-mode file handle) so no platform ever silently swaps in `\\r\\n` --
AGENTS.md requires byte-identical output on a rerun, and a daily file must never carry a
wall-clock field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def dump_json_bytes(obj: Any) -> bytes:
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_json_bytes(obj))


def write_daily(out_dir: Path, day_str: str, payload: dict) -> None:
    write_json(out_dir / "daily" / f"{day_str}.json", payload)


def rebuild_index(out_dir: Path) -> None:
    """Rebuild `index.json` from whatever `.json` files already exist under `daily/` on
    disk -- not just the current run's `--since`/`--until` window -- so a later, narrower
    run never shrinks the index (plan-critic note on plan #9)."""
    daily_dir = out_dir / "daily"
    days = sorted(p.stem for p in daily_dir.glob("*.json")) if daily_dir.exists() else []
    write_json(out_dir / "index.json", {"schema_version": 1, "days": days})
