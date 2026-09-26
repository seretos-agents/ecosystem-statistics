"""R1 (plan #12) -- the `rounds` block in `daily/<day>.json`, end to end through
`collect` over a `MockTransport` standing in for GitHub's REST API (never a real network
call -- AGENTS.md), mirroring `test_collect_escalations.py`'s (#11) structure.

Fixture (verbatim from the plan): `--since 2024-03-04(D1) --until 2024-03-05(D2)`, one repo
(`acme/demo`), four tickets:

    #1 (dev): started D1 09:00; ci-green D1 12:00 with
        plan-critic=2/3(1f,0i) test-critic=1/3(0f,0i) review=4/3(2f,1i) ci=1/3(0f,0i)
        rebase=0/3(0f,0i).
    #2 (dev): started D1 09:00; replan-triggered D1 10:00; blocked D1 11:00 with
        plan-critic=3/3(2f,0i) and every other gate 0/3(0f,0i); started D2 09:00;
        ci-green D2 15:00 with
        plan-critic=1/3(0f,0i) test-critic=2/3(1f,1i) review=1/3(0f,0i) ci=2/3(0f,1i)
        rebase=0/3(0f,0i).
    #3 (prose, old era): started D1; failed D1 with
        scenario-critic=2/3(1f,1i) evidence=1/3(0f,1i) review=0/3(0f,0i) ci=0/3(0f,0i)
        rebase=0/3(0f,0i).
    #4 (prose, new era): started D2 08:00; failed D2 10:00 with
        scenario-critic=4/3(1f,1i) evidence=2/3(0f,1i) and every other gate 0/3(0f,0i).

Expected RED reason: `KeyError: 'rounds'` -- `collect.py` does not produce this key yet
(only `branch_churn`, `main_rework` and #11's `escalations`), so the first assertion below
fails for exactly that reason.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, adev_event_block, read_tree

D1 = date(2024, 3, 4)
D2 = date(2024, 3, 5)


def _ts(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z"


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


# ---------------------------------------------------------------------------
# The fixture: four tickets -- two dev sessions across a restart (#1, #2), one
# old-era prose ticket (#3) and one new-era prose ticket (#4).
# ---------------------------------------------------------------------------

_ISSUES_META = [
    {"number": 1, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 2, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 3, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 4, "created_at": _ts(D2, 7, 0), "closed_at": None},
]

_COMMENTS_BY_NUMBER: dict[int, list[tuple[str, str]]] = {
    1: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (
            _ts(D1, 12, 0),
            adev_event_block(
                "ci-green",
                rounds=(
                    "plan-critic=2/3(1f,0i) test-critic=1/3(0f,0i) review=4/3(2f,1i) "
                    "ci=1/3(0f,0i) rebase=0/3(0f,0i)"
                ),
            ),
        ),
    ],
    2: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 10, 0), adev_event_block("replan-triggered")),
        (
            _ts(D1, 11, 0),
            adev_event_block(
                "blocked",
                rounds=(
                    "plan-critic=3/3(2f,0i) test-critic=0/3(0f,0i) review=0/3(0f,0i) "
                    "ci=0/3(0f,0i) rebase=0/3(0f,0i)"
                ),
            ),
        ),
        (_ts(D2, 9, 0), adev_event_block("started")),
        (
            _ts(D2, 15, 0),
            adev_event_block(
                "ci-green",
                rounds=(
                    "plan-critic=1/3(0f,0i) test-critic=2/3(1f,1i) review=1/3(0f,0i) "
                    "ci=2/3(0f,1i) rebase=0/3(0f,0i)"
                ),
            ),
        ),
    ],
    3: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (
            _ts(D1, 10, 0),
            adev_event_block(
                "failed",
                rounds=(
                    "scenario-critic=2/3(1f,1i) evidence=1/3(0f,1i) review=0/3(0f,0i) "
                    "ci=0/3(0f,0i) rebase=0/3(0f,0i)"
                ),
            ),
        ),
    ],
    4: [
        (_ts(D2, 8, 0), adev_event_block("started")),
        (
            _ts(D2, 10, 0),
            adev_event_block(
                "failed",
                rounds=(
                    "scenario-critic=4/3(1f,1i) evidence=2/3(0f,1i) review=0/3(0f,0i) "
                    "ci=0/3(0f,0i) rebase=0/3(0f,0i)"
                ),
            ),
        ),
    ],
}


def _make_client_factory():
    def make_client(token: str | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/issues"):
                assert request.url.params.get("state") == "all"
                return httpx.Response(200, json=_ISSUES_META)
            if "/issues/" in path and path.endswith("/comments"):
                number = int(path.rsplit("/issues/", 1)[1].split("/", 1)[0])
                body = [
                    {"created_at": created_at, "body": comment_body}
                    for created_at, comment_body in _COMMENTS_BY_NUMBER[number]
                ]
                return httpx.Response(200, json=body)
            assert "/pulls" in path
            return httpx.Response(200, json=[])

        return httpx.Client(transport=httpx.MockTransport(handler))

    return make_client


def _read_day(out_dir: Path, day: date) -> dict:
    return json.loads((out_dir / "daily" / f"{day.isoformat()}.json").read_text(encoding="utf-8"))


def _run_collect(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, out_dir: Path, cache_dir: Path,
    *, since: str, until: str,
) -> int:
    from ecosystem_statistics.__main__ import main
    import ecosystem_statistics.github as github

    monkeypatch.setattr(github, "make_client", _make_client_factory())
    monkeypatch.chdir(workdir)

    return main(
        [
            "collect",
            "--since", since,
            "--until", until,
            "--out", str(out_dir),
            "--cache-dir", str(cache_dir),
        ]
    )


def test_rounds_block_in_daily_json(
    tmp_path: Path, synthetic_repo: SyntheticRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = synthetic_repo
    origin.commit_file(
        "a.py", "a\n", "init", when=datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    )

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", origin.clone_url())

    out_dir = workdir / "out"
    cache_dir = workdir / "cache"
    assert _run_collect(
        monkeypatch, workdir, out_dir, cache_dir, since="2024-03-04", until="2024-03-05"
    ) == 0

    day1 = _read_day(out_dir, D1)
    day2 = _read_day(out_dir, D2)

    # --- D1 -----------------------------------------------------------------
    totals1 = day1["totals"]["rounds"]
    assert totals1["replan_triggered"] == 1
    assert totals1["sessions"] == {"tickets": 3, "started": 3, "per_ticket": 1.0}

    dev1 = totals1["lanes"]["dev"]
    assert dev1["counting"] == "used"
    assert dev1["sessions"] == 2
    assert dev1["gates"]["plan-critic"]["used"] == {"avg": 2.5, "median": 2.5, "p90": 3}
    assert dev1["gates"]["plan-critic"]["f"] == {"avg": 1.5, "median": 1.5, "p90": 2}
    assert dev1["gates"]["review"]["used"] == {"avg": 2.0, "median": 2.0, "p90": 4}
    assert dev1["gates"]["review"]["i"] == {"avg": 0.5, "median": 0.5, "p90": 1}
    assert dev1["over_soft_cap_share"] == 0.2
    assert dev1["infra_share"] == 0.0909

    prose1 = totals1["lanes"]["prose"]
    assert prose1["counting"] == "f+i"
    assert prose1["sessions"] == 1
    assert prose1["gates"]["scenario-critic"]["used"] == {"avg": 2.0, "median": 2.0, "p90": 2}
    assert prose1["over_soft_cap_share"] == 0.0
    assert prose1["infra_share"] == 0.6667

    # per_repo is byte-for-byte the same as totals -- there is only one repo.
    assert day1["per_repo"]["acme/demo"]["rounds"] == totals1

    # --- D2 -------------------------------------------------------------------
    totals2 = day2["totals"]["rounds"]
    assert totals2["replan_triggered"] == 0
    # Both max(attempt) and bucketing sessions by their `started` day would give
    # per_ticket 1.0 here -- 1.5 is only reachable by anchoring on the terminal event
    # and counting every `started` up to it (plan #12, F2).
    assert totals2["sessions"] == {"tickets": 2, "started": 3, "per_ticket": 1.5}

    dev2 = totals2["lanes"]["dev"]
    assert dev2["sessions"] == 1
    assert dev2["over_soft_cap_share"] == 0.0
    assert dev2["infra_share"] == 0.3333

    prose2 = totals2["lanes"]["prose"]
    assert prose2["sessions"] == 1
    # Reading the prose `used` field literally would give 4 (scenario-critic) and 2
    # (evidence); the `f+i` counting scheme gives 2 and 1 instead.
    assert prose2["gates"]["scenario-critic"]["used"] == {"avg": 2.0, "median": 2.0, "p90": 2}
    assert prose2["gates"]["evidence"]["used"] == {"avg": 1.0, "median": 1.0, "p90": 1}
    # Over-soft-cap under the real (f+i) counting is 0.0; the literal `used` field
    # would instead put scenario-critic's 4 over the soft cap of 3, giving 0.5.
    assert prose2["over_soft_cap_share"] == 0.0
    assert prose2["infra_share"] == 0.6667

    assert day2["per_repo"]["acme/demo"]["rounds"] == totals2

    # A rerun into a second --out is byte-identical (AGENTS.md: reproducible output).
    out_dir2 = workdir / "out2"
    cache_dir2 = workdir / "cache2"
    assert _run_collect(
        monkeypatch, workdir, out_dir2, cache_dir2, since="2024-03-04", until="2024-03-05"
    ) == 0
    assert read_tree(out_dir) == read_tree(out_dir2)


def test_since_d2_only_writes_the_same_d2_bytes(
    tmp_path: Path, synthetic_repo: SyntheticRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions are anchored on the ticket's full history up to `--until` (plan #12:
    "the full history up to --until is fetched, so n does not depend on the window") --
    a `--since D2 --until D2` run must write the same D2 bytes as the wider D1..D2 run
    above. This may already pass once the wider run does (additional edge-case
    coverage, not a second driving test)."""
    origin = synthetic_repo
    origin.commit_file(
        "a.py", "a\n", "init", when=datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    )

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", origin.clone_url())

    out_dir = workdir / "out"
    cache_dir = workdir / "cache"
    assert _run_collect(
        monkeypatch, workdir, out_dir, cache_dir, since="2024-03-04", until="2024-03-05"
    ) == 0

    out_d2_only = workdir / "out_d2_only"
    cache_d2_only = workdir / "cache_d2_only"
    assert _run_collect(
        monkeypatch, workdir, out_d2_only, cache_d2_only, since="2024-03-05", until="2024-03-05"
    ) == 0

    assert (out_d2_only / "daily" / "2024-03-05.json").read_bytes() == (
        out_dir / "daily" / "2024-03-05.json"
    ).read_bytes()
