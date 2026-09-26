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
        # plan #11 adds an `/issues` fetch alongside `/pulls`; answer it with no
        # issues so this file's churn/rework-only tests are unaffected.
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[])
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

    daily_bytes = snapshot_a["daily/2024-01-01.json"]
    assert daily_bytes.endswith(b"\n")
    # `endswith(b"\n")` alone would also pass for CRLF line endings -- rule those out
    # directly rather than relying on a suffix check that CRLF satisfies too.
    assert b"\r" not in daily_bytes

    day1_payload = json.loads(daily_bytes)
    # Assert the full expected top-level key set (not just the absence of the literal
    # `generated_at` key) so any wall-clock or other stray field is caught, whatever it
    # is named.
    assert set(day1_payload.keys()) == {"schema_version", "date", "totals", "per_repo"}
    assert day1_payload["schema_version"] == 1
    assert day1_payload["date"] == "2024-01-01"
    assert set(day1_payload["totals"].keys()) == {
        "branch_churn", "main_rework", "escalations", "rounds", "regression_chains",
        "throughput", "clarification",
    }
    # No merged PRs (mocked to return none) -> branch_churn totals are all zero/null.
    assert day1_payload["totals"]["branch_churn"] == {
        "gross_added": 0,
        "net_added": 0,
        "prs": 0,
        "value": None,
    }
    # day1's commit adds 5 lines to a.py within the 21-day window and removes nothing.
    assert day1_payload["totals"]["main_rework"] == {
        "young_removed": 0,
        "added_to_main": 5,
        "window_days": 21,
        "value": 0.0,
    }
    assert set(day1_payload["per_repo"].keys()) == {"acme/demo"}
    repo_block = day1_payload["per_repo"]["acme/demo"]
    assert repo_block["branch_churn"]["prs"] == 0
    assert repo_block["branch_churn"]["per_pr"] == []
    assert repo_block["main_rework"] == day1_payload["totals"]["main_rework"]

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


def test_index_is_sorted_regardless_of_collection_order(
    tmp_path: Path, synthetic_repo: SyntheticRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """index.json must end up sorted even when a later day is collected before an
    earlier one -- collecting day1-then-day2 in chronological order (as the
    byte-identical-rerun test above does) would also pass an append-without-sort
    implementation (test-critic F6)."""
    origin = synthetic_repo
    origin.commit_file(
        "a.py",
        "a\n",
        "init",
        when=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", origin.clone_url())
    monkeypatch.chdir(workdir)

    from ecosystem_statistics.__main__ import main
    import ecosystem_statistics.github as github

    monkeypatch.setattr(github, "make_client", _no_prs_client)

    out_dir = workdir / "out"
    cache_dir = workdir / "cache"

    assert main(
        [
            "collect",
            "--since", "2024-01-05",
            "--until", "2024-01-05",
            "--out", str(out_dir),
            "--cache-dir", str(cache_dir),
        ]
    ) == 0
    assert main(
        [
            "collect",
            "--since", "2024-01-02",
            "--until", "2024-01-02",
            "--out", str(out_dir),
            "--cache-dir", str(cache_dir),
        ]
    ) == 0

    index = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    assert index["days"] == ["2024-01-02", "2024-01-05"]


def test_pr_list_request_carries_real_default_branch(
    tmp_path: Path, synthetic_repo: SyntheticRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PR-listing request must carry `base=<repo's actual default branch>` (plan #9:
    "`GET /repos/{o}/{r}/pulls?state=closed&base=<default_branch>...`"). Proven with a repo
    whose default branch is `trunk`, not `main` -- a `list_merged_prs` call that hardcoded
    `base="main"` (the likeliest wrong constant) would pass a same-named-branch test but
    fail this one.
    """
    origin = synthetic_repo
    origin.commit_file(
        "a.py", "a\n", "init", when=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    origin.rename_branch("main", "trunk")

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", origin.clone_url())
    monkeypatch.chdir(workdir)

    from ecosystem_statistics.__main__ import main
    import ecosystem_statistics.github as github

    seen_bases: list[str | None] = []

    def make_capturing_client(token: str | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            # plan #11 adds an `/issues` fetch alongside `/pulls`; answer it with no
            # issues so it never reaches the `base=` assertion below.
            if request.url.path.endswith("/issues"):
                return httpx.Response(200, json=[])
            assert "/pulls" in request.url.path
            seen_bases.append(request.url.params.get("base"))
            return httpx.Response(200, json=[])

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(github, "make_client", make_capturing_client)

    out_dir = workdir / "out"
    cache_dir = workdir / "cache"

    assert main(
        [
            "collect",
            "--since", "2024-01-01",
            "--until", "2024-01-01",
            "--out", str(out_dir),
            "--cache-dir", str(cache_dir),
        ]
    ) == 0

    assert seen_bases == ["trunk"]
