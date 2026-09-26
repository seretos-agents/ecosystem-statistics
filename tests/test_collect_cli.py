"""R3 — CLI writes deterministic daily files + a cumulative index (plan #9).

`python -m ecosystem_statistics collect --since --until --out` must produce byte-identical
`daily/<day>.json` files and `index.json` on a rerun over the same window (AGENTS.md:
"Output must be idempotent and reproducible ... byte-identical JSON on a re-run"). It must
also rebuild `index.json` from whatever is already on disk under `daily/`, not just from
the current `--since`/`--until` window (plan-critic note on plan #9) — so a second,
non-overlapping collect into the same `--out` accumulates rather than clobbering earlier
days.

Uses a `file://` clone of a synthetic repo (never a real GitHub clone) and a `MockTransport`
in place of `ecosystem_statistics.github.make_client` (the seam the plan names for R3), so
the whole run is offline and deterministic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, read_tree


def _write_config(config_dir: Path, clone_url: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "repos.yml").write_text(
        "owner: acme\n"
        "repos:\n"
        "  - name: demo\n"
        f"    clone_url: {clone_url}\n",
        encoding="utf-8",
    )
    (config_dir / "churn.yml").write_text(
        "rework_window_days: 21\n"
        "exclude_paths:\n"
        '  - "*.lock"\n'
        "  - package-lock.json\n"
        "exclude_commits: []\n",
        encoding="utf-8",
    )


def _no_prs_client(token: str | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/pulls" in request.url.path
        return httpx.Response(200, json=[])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rerun_is_byte_identical(
    tmp_path: Path, synthetic_repo: SyntheticRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = synthetic_repo
    day1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    day2 = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    origin.commit_file(
        "a.py", "\n".join(f"a{i}" for i in range(5)) + "\n", "day1: add a.py", when=day1
    )
    origin.commit_file(
        "a.py", "\n".join(f"a{i}" for i in range(7)) + "\n", "day2: add 2 lines", when=day2
    )

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", origin.clone_url())
    monkeypatch.chdir(workdir)

    # Import the module under test first, so a missing-module RED is attributed to it
    # rather than to whichever sibling module a later import statement happens to touch.
    from ecosystem_statistics.__main__ import main
    import ecosystem_statistics.github as github

    monkeypatch.setattr(github, "make_client", _no_prs_client)

    out_a = workdir / "out_a"
    out_b = workdir / "out_b"
    cache_a = workdir / "cache_a"
    cache_b = workdir / "cache_b"

    argv_day1 = [
        "collect",
        "--since", "2024-01-01",
        "--until", "2024-01-01",
        "--out", str(out_a),
        "--cache-dir", str(cache_a),
    ]
    assert main(argv_day1) == 0

    argv_day1_into_b = [
        "collect",
        "--since", "2024-01-01",
        "--until", "2024-01-01",
        "--out", str(out_b),
        "--cache-dir", str(cache_b),
    ]
    assert main(argv_day1_into_b) == 0

    snapshot_a = read_tree(out_a)
    snapshot_b = read_tree(out_b)
    assert snapshot_a == snapshot_b
    assert "daily/2024-01-01.json" in snapshot_a
    assert "index.json" in snapshot_a
    assert snapshot_a["daily/2024-01-01.json"].endswith(b"\n")
    assert b"generated_at" not in snapshot_a["daily/2024-01-01.json"]

    # Rerun into out_a with the same cache: must reproduce the exact same bytes.
    assert main(argv_day1) == 0
    assert read_tree(out_a) == snapshot_a

    # A second, disjoint window collected into the SAME --out must accumulate: index.json
    # is rebuilt from daily/ on disk, not just from this call's window, and the earlier
    # day's file is left byte-for-byte untouched.
    argv_day2 = [
        "collect",
        "--since", "2024-01-02",
        "--until", "2024-01-02",
        "--out", str(out_a),
        "--cache-dir", str(cache_a),
    ]
    assert main(argv_day2) == 0

    index = json.loads((out_a / "index.json").read_text(encoding="utf-8"))
    assert index["days"] == ["2024-01-01", "2024-01-02"]
    assert (
        out_a / "daily" / "2024-01-01.json"
    ).read_bytes() == snapshot_a["daily/2024-01-01.json"]
