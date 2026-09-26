"""R1 (plan #14) -- the `throughput` block in `daily/<day>.json`, end to end through
`collect` over a `MockTransport` standing in for GitHub's REST API (never a real network
call -- AGENTS.md), mirroring `test_collect_rounds.py`'s (#12) and
`test_collect_regression_chains.py`'s (#13) structure.

Fixture (verbatim from the plan, round 3): `--since 2024-03-04 (D1) --until 2024-03-05
(D2)`, one repo (`acme/demo`), seven tickets (created time in brackets):

    #1 [03-01 00:00]: Released `Package: single #1 -- single` D1 02:00, `started` D1
        04:00, `pr-opened` D1 07:00, `ci-green pr: 42` D1 11:00. Closed D2 03:00,
        completed.
    #2 [D1 01:00]: `started` D1 05:00. Closed D1 09:00, completed, no ci-green.
    #3 [03-01]: closed D2 10:00, not_planned, no ci-green.
    #4 `chore(deps): bump httpx` [D2 01:00]: closed D2 02:00, completed, no ci-green.
    #5 [D1 00:00] and #6 [D2 00:00]: Released `Package: bundle #5 -- collision` D2 00:00
        and D2 06:00.
    #7 [03-03 12:00]: Released `Package: epic #7 -- effort` D2 12:00.

PR #42 is merged D2 03:00, backed by a real two-parent `SyntheticRepo.merge(...)` commit
(the collector's branch_churn path needs a genuine two-parent merge commit to not skip
the PR -- see `_collect_repo`'s "not a two-parent merge commit" guard -- even though this
file makes no assertion on branch_churn itself).

Expected RED reason: `KeyError: 'throughput'` -- `collect.py` does not produce this key
yet (only `branch_churn`, `main_rework`, #11's `escalations`, #12's `rounds` and #13's
`regression_chains`), so the first assertion below fails for exactly that reason. No
source change is needed to reach this RED: the mocked `/issues` payload's extra
`title`/`state_reason` fields are silently ignored by today's `github.list_issues` (a
4-tuple return), and `run_collect` completes normally without ever writing the key.
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


def _released(package_line: str) -> str:
    return f"## Released (gatekeeper)\n\nPackage: {package_line}\nChecked: n/a.\n"


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
# The fixture: seven tickets covering a full cycle (#1), a manual close with no
# ci-green (#2), a not_planned manual close (#3), a chore(deps) bump (#4), a bundled
# package released by two members (#5/#6) and a single-member epic package (#7).
# ---------------------------------------------------------------------------

_ISSUES_META = [
    {
        "number": 1,
        "created_at": "2024-03-01T00:00:00Z",
        "closed_at": _ts(D2, 3, 0),
        "state_reason": "completed",
        "title": "Ticket one",
    },
    {
        "number": 2,
        "created_at": _ts(D1, 1, 0),
        "closed_at": _ts(D1, 9, 0),
        "state_reason": "completed",
        "title": "Ticket two",
    },
    {
        "number": 3,
        "created_at": "2024-03-01T00:00:00Z",
        "closed_at": _ts(D2, 10, 0),
        "state_reason": "not_planned",
        "title": "Ticket three",
    },
    {
        "number": 4,
        "created_at": _ts(D2, 1, 0),
        "closed_at": _ts(D2, 2, 0),
        "state_reason": "completed",
        "title": "chore(deps): bump httpx",
    },
    {
        "number": 5,
        "created_at": _ts(D1, 0, 0),
        "closed_at": None,
        "state_reason": None,
        "title": "Ticket five",
    },
    {
        "number": 6,
        "created_at": _ts(D2, 0, 0),
        "closed_at": None,
        "state_reason": None,
        "title": "Ticket six",
    },
    {
        "number": 7,
        "created_at": "2024-03-03T12:00:00Z",
        "closed_at": None,
        "state_reason": None,
        "title": "Ticket seven",
    },
]

_COMMENTS_BY_NUMBER: dict[int, list[tuple[str, str]]] = {
    1: [
        (_ts(D1, 2, 0), _released("single #1 — single")),
        (_ts(D1, 4, 0), adev_event_block("started")),
        (_ts(D1, 7, 0), adev_event_block("pr-opened")),
        (_ts(D1, 11, 0), adev_event_block("ci-green", pr="42")),
    ],
    2: [
        (_ts(D1, 5, 0), adev_event_block("started")),
    ],
    3: [],
    4: [],
    5: [
        (_ts(D2, 0, 0), _released("bundle #5 — collision")),
    ],
    6: [
        (_ts(D2, 6, 0), _released("bundle #5 — collision")),
    ],
    7: [
        (_ts(D2, 12, 0), _released("epic #7 — effort")),
    ],
}


def _build_repo_with_merged_pr(origin: SyntheticRepo) -> str:
    """A real two-parent merge commit for PR #42, merged D2 03:00 -- `_collect_repo`
    skips (with a stderr note, not a crash) any PR whose `merge_commit_sha` is not a
    two-parent merge, so the throughput fixture needs a genuine one even though this
    file asserts nothing about branch_churn itself."""
    origin.commit_file(
        "a.py", "a\n", "init", when=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    origin.branch("feature", start_point="main")
    origin.commit_file(
        "feature.py",
        "f\n",
        "feature: add file",
        when=datetime(2024, 3, 4, 12, 0, 0, tzinfo=timezone.utc),
    )
    origin.checkout("main")
    merge_sha = origin.merge(
        "feature",
        "Merge pull request #42 from feature",
        when=datetime(2024, 3, 5, 3, 0, 0, tzinfo=timezone.utc),
    )
    return merge_sha


def _make_client_factory(merge_sha: str):
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
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 42,
                        "updated_at": _ts(D2, 3, 0),
                        "merged_at": _ts(D2, 3, 0),
                        "merge_commit_sha": merge_sha,
                    }
                ],
            )

        return httpx.Client(transport=httpx.MockTransport(handler))

    return make_client


def _read_day(out_dir: Path, day: date) -> dict:
    return json.loads((out_dir / "daily" / f"{day.isoformat()}.json").read_text(encoding="utf-8"))


def _run_collect(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, out_dir: Path, cache_dir: Path,
    merge_sha: str, *, since: str, until: str,
) -> int:
    from ecosystem_statistics.__main__ import main
    import ecosystem_statistics.github as github

    monkeypatch.setattr(github, "make_client", _make_client_factory(merge_sha))
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


_STAGE_NAMES = [
    "created_to_released",
    "released_to_started",
    "started_to_pr_opened",
    "pr_opened_to_ci_green",
    "ci_green_to_done",
]


def _empty_stage() -> dict:
    return {"n": 0, "median": None, "p90": None}


def test_throughput_block_in_daily_json(
    tmp_path: Path, synthetic_repo: SyntheticRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = synthetic_repo
    merge_sha = _build_repo_with_merged_pr(origin)

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", origin.clone_url())

    out_dir = workdir / "out"
    cache_dir = workdir / "cache"
    assert _run_collect(
        monkeypatch, workdir, out_dir, cache_dir, merge_sha,
        since="2024-03-04", until="2024-03-05",
    ) == 0

    day1 = _read_day(out_dir, D1)
    day2 = _read_day(out_dir, D2)

    expected_day1 = {
        "events": {"released": 1, "started": 2, "ci_green": 1},
        "merged_prs": 0,
        "closed": {"completed": 1, "not_planned": 0, "other": 0, "pipeline": 0, "manual": 1},
        "stages": {
            **{name: _empty_stage() for name in _STAGE_NAMES},
            "created_to_released": {"n": 1, "median": 74.0, "p90": 74.0},
            "released_to_started": {"n": 1, "median": 2.0, "p90": 2.0},
            "started_to_pr_opened": {"n": 1, "median": 3.0, "p90": 3.0},
            "pr_opened_to_ci_green": {"n": 1, "median": 4.0, "p90": 4.0},
        },
        "packages": {"total": 1, "bundled": 0, "bundled_share": 0.0, "by_reason": {"single": 1}},
        "dependency_bumps": {"created": 0, "closed": 0},
    }
    expected_day2 = {
        "events": {"released": 3, "started": 0, "ci_green": 0},
        "merged_prs": 1,
        "closed": {"completed": 2, "not_planned": 1, "other": 0, "pipeline": 1, "manual": 2},
        "stages": {
            **{name: _empty_stage() for name in _STAGE_NAMES},
            "created_to_released": {"n": 3, "median": 24.0, "p90": 48.0},
            "ci_green_to_done": {"n": 1, "median": 16.0, "p90": 16.0},
        },
        "packages": {
            "total": 2, "bundled": 2, "bundled_share": 1.0,
            "by_reason": {"collision": 1, "effort": 1},
        },
        "dependency_bumps": {"created": 1, "closed": 1},
    }

    assert day1["totals"]["throughput"] == expected_day1
    assert day2["totals"]["throughput"] == expected_day2
    # per_repo is byte-for-byte the same as totals -- there is only one repo.
    assert day1["per_repo"]["acme/demo"]["throughput"] == expected_day1
    assert day2["per_repo"]["acme/demo"]["throughput"] == expected_day2

    # A rerun into a second --out is byte-identical (AGENTS.md: reproducible output).
    # Compared as raw bytes, not parsed JSON, per plan-critic's note that a parsed-JSON
    # comparison could pass despite key-order/formatting differences a byte compare
    # would catch.
    out_dir2 = workdir / "out2"
    cache_dir2 = workdir / "cache2"
    assert _run_collect(
        monkeypatch, workdir, out_dir2, cache_dir2, merge_sha,
        since="2024-03-04", until="2024-03-05",
    ) == 0
    tree1 = read_tree(out_dir)
    tree2 = read_tree(out_dir2)
    assert tree1.keys() == tree2.keys()
    for name, content in tree1.items():
        assert isinstance(content, bytes)
        assert content == tree2[name]
