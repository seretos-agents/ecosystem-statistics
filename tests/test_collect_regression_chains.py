"""R1/R3 (plan #13) -- the `regression_chains` block in `daily/<day>.json`, end to end
through `collect` over a `MockTransport` standing in for GitHub's REST API (never a real
network call -- AGENTS.md), mirroring `test_collect_escalations.py`'s (#11) and
`test_collect_rounds.py`'s (#12) structure.

Fixture (verbatim from the plan): `--since 2024-03-04 (D1) --until 2024-03-05 (D2)`, one
repo (`acme/demo`), `/pulls` returns `[]`. All tickets are created before D1. "(L)" means
the ticket carries the `regression-chain` label.

    #1 (L): on D1, a chain heading with a table only (#3, #1, acme/other-repo#47); on D2,
        a plain comment.
    #2 (L): on D2, a chain heading, a table (#2, #5, acme/other-repo#47) and a block
        `members: acme/demo#2, acme/demo#5, acme/other-repo#47`.
    #3: on D1, an `adev:event v1` `started` block.
    #4: `closed_at` on D2, no comments.
    #5: on D2, `## Released (gatekeeper)`.
    #6: nothing.
    #7 (L): on D2, an `adev:event v1` block.

Expected RED reason: `KeyError: 'regression_chains'` -- `collect.py` does not produce this
key yet (only `branch_churn`, `main_rework`, #11's `escalations` and #12's `rounds`), so
the first assertion below fails for exactly that reason. No source change is needed to
reach this RED: the mocked `/issues` payload's extra `labels` field is silently ignored by
today's `github.list_issues` (a 3-tuple return), and none of the chain-comment bodies
below contain an `adev`/`ato` machine block, so `run_collect` completes normally and simply
never writes the key.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, adev_event_block, chain_block, chain_table, read_tree
from conftest import CHAIN_HEADING

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
# The fixture: seven tickets, three of which carry the regression-chain label.
# ---------------------------------------------------------------------------

_LABELLED = [{"name": "regression-chain"}]

_ISSUES_META = [
    {"number": 1, "created_at": _ts(D1, 8, 0), "closed_at": None, "labels": _LABELLED},
    {"number": 2, "created_at": _ts(D1, 8, 0), "closed_at": None, "labels": _LABELLED},
    {"number": 3, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 4, "created_at": _ts(D1, 8, 0), "closed_at": _ts(D2, 15, 0)},
    {"number": 5, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 6, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 7, "created_at": _ts(D1, 8, 0), "closed_at": None, "labels": _LABELLED},
]

_COMMENTS_BY_NUMBER: dict[int, list[tuple[str, str]]] = {
    1: [
        (
            _ts(D1, 10, 0),
            CHAIN_HEADING + "\n\n" + chain_table(["#3", "#1", "acme/other-repo#47"]),
        ),
        (_ts(D2, 9, 0), "Just checking in, no update yet.\n"),
    ],
    2: [
        (
            _ts(D2, 10, 0),
            CHAIN_HEADING
            + "\n\n"
            + chain_table(["#2", "#5", "acme/other-repo#47"])
            + "\n"
            + chain_block("acme/demo#2, acme/demo#5, acme/other-repo#47"),
        ),
    ],
    3: [
        (_ts(D1, 9, 0), adev_event_block("started")),
    ],
    4: [],
    5: [
        (_ts(D2, 9, 0), "## Released (gatekeeper)\n\nShipped.\n"),
    ],
    6: [],
    7: [
        (_ts(D2, 9, 0), adev_event_block("started")),
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


def test_regression_chains_block_in_daily_json(
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

    expected_day1 = {
        "active": 2,
        "chain_active": 1,
        "value": 0.5,
        "detected": 1,
        "chains": [{"ticket": "acme/demo#1", "length": 3}],
    }
    expected_day2 = {
        "active": 4,
        "chain_active": 2,
        "value": 0.5,
        "detected": 1,
        "chains": [{"ticket": "acme/demo#2", "length": 3}],
    }

    assert day1["totals"]["regression_chains"] == expected_day1
    assert day2["totals"]["regression_chains"] == expected_day2
    # per_repo is byte-for-byte the same as totals -- there is only one repo.
    assert day1["per_repo"]["acme/demo"]["regression_chains"] == expected_day1
    assert day2["per_repo"]["acme/demo"]["regression_chains"] == expected_day2

    # A rerun into a second --out is byte-identical (AGENTS.md: reproducible output).
    out_dir2 = workdir / "out2"
    cache_dir2 = workdir / "cache2"
    assert _run_collect(
        monkeypatch, workdir, out_dir2, cache_dir2, since="2024-03-04", until="2024-03-05"
    ) == 0
    assert read_tree(out_dir) == read_tree(out_dir2)


# ---------------------------------------------------------------------------
# R3: _merge_regression_chains sums across repos, recomputes value, sorts chains
# ---------------------------------------------------------------------------
#
# Expected RED reason: `ImportError` -- `_merge_regression_chains` does not exist yet in
# `ecosystem_statistics.collect`.
#
# The fixture is deliberately two-repo with overlapping-looking but distinct chains, so a
# naive `blocks[-1]` (last-write-wins) implementation would produce different
# active/chain_active/value/chains than the real sum below -- mirroring
# `test_merge_escalations_sums_across_repos_not_last_write_wins`'s (#11) shape.


def test_merge_regression_chains_sums_across_repos_not_last_write_wins() -> None:
    from ecosystem_statistics.collect import _merge_regression_chains

    repo_a = {
        "active": 3,
        "chain_active": 1,
        "value": 0.3333,
        "detected": 1,
        "chains": [{"ticket": "acme/demo#5", "length": 2}],
    }
    repo_b = {
        "active": 5,
        "chain_active": 2,
        "value": 0.4,
        "detected": 2,
        "chains": [
            {"ticket": "acme/other#9", "length": 1},
            {"ticket": "acme/other#1", "length": 4},
        ],
    }

    merged = _merge_regression_chains([repo_a, repo_b])

    assert merged == {
        "active": 8,
        "chain_active": 3,
        "value": 0.375,
        "detected": 3,
        "chains": [
            {"ticket": "acme/demo#5", "length": 2},
            {"ticket": "acme/other#1", "length": 4},
            {"ticket": "acme/other#9", "length": 1},
        ],
    }
    # A naive `blocks[-1]` (last-write-wins) copy would equal repo_b exactly, including
    # its unsorted chains list -- confirm the real merge differs from it.
    assert merged != repo_b
