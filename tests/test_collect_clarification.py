"""R1/R3 (plan #17) -- the `clarification` block in `daily/<day>.json`, end to end through
`collect` over a `MockTransport` standing in for GitHub's REST API (never a real network
call -- AGENTS.md), modelled on `test_collect_regression_chains.py`'s (#13) structure.

Fixture (verbatim from the plan): `--since 2024-03-04 (D1) --until 2024-03-05 (D2)`, one
repo (`acme/demo`), `/pulls` returns `[]`. All headings carry the `(gatekeeper)` suffix
unless noted otherwise.

    #1: D1 Clarification with 2 Qs, then D2 Released `single #1`.
    #2: D1 Released `single #2`.
    #3: D2 Clarification with 4 Qs.
    #4: D1 Frame with block `yes/3/1` and one `Not proven by this package:` line. D2
        Frame without a block, with two `- Not proven by this package:` lines and one
        mid-line mention.
    #5: D2 Lane split; D2 Re-cut; a D2 comment with `- PREMISE FALSIFIED: x` plus one
        mid-sentence mention.
    #6: a D1 plain comment and a D1 `## Released in v1.2` (no gatekeeper suffix).
    #7: a Clarification on 2024-03-01 (before `--since`; feeds state, never counted),
        then a D1 Released `single #7`.
    #8: a D1 Clarification with 1 Q.
    #9 and #10: each gets the same D2 Released `Package: epic #9 (children #8, #10) --
        collision`.

Expected RED reason: `KeyError: 'clarification'` -- `collect.py` does not produce this key
yet (only `branch_churn`, `main_rework`, #11's `escalations`, #12's `rounds`, #13's
`regression_chains` and #14's `throughput`), so the first assertion below fails for
exactly that reason. No source change is needed to reach this RED: none of the fixture
comment bodies below contain an `adev`/`ato` machine block or a regression-chain heading,
so `run_collect` completes normally and simply never writes the `clarification` key.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, clarification_comment, read_tree, released_comment

D1 = date(2024, 3, 4)
D2 = date(2024, 3, 5)
BEFORE_SINCE = date(2024, 2, 25)


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
# The fixture: ten tickets exercising every counter in the plan's R1 table.
# ---------------------------------------------------------------------------

_ISSUES_META = [
    {"number": 1, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 2, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 3, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 4, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 5, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 6, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 7, "created_at": _ts(BEFORE_SINCE, 8, 0), "closed_at": None},
    {"number": 8, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 9, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 10, "created_at": _ts(D1, 8, 0), "closed_at": None},
]

_FRAME_D1 = (
    "## Frame (gatekeeper)\n\n"
    "- Not proven by this package: reason A\n\n"
    "<!-- gatekeeper:frame v1\n"
    "ac_rewritten: yes\n"
    "premises: 3\n"
    "not_proven: 1\n"
    "-->\n"
)

_FRAME_D2 = (
    "## Frame (gatekeeper)\n\n"
    "- Not proven by this package: reason B\n"
    "- Not proven by this package: reason C\n"
    "We considered whether Not proven by this package: applies mid-sentence here too.\n"
)

_PREMISE_FALSIFIED_D2 = (
    "- PREMISE FALSIFIED: x\n"
    "We discussed PREMISE FALSIFIED: mid-sentence, which does not count.\n"
)

_PACKAGE_LINE_9_10 = "epic #9 (children #8, #10) — collision"

_COMMENTS_BY_NUMBER: dict[int, list[tuple[str, str]]] = {
    1: [
        (_ts(D1, 9, 30), clarification_comment(2)),
        (_ts(D2, 9, 0), released_comment("single #1")),
    ],
    2: [
        (_ts(D1, 10, 0), released_comment("single #2")),
    ],
    3: [
        (_ts(D2, 9, 30), clarification_comment(4)),
    ],
    4: [
        (_ts(D1, 10, 30), _FRAME_D1),
        (_ts(D2, 10, 0), _FRAME_D2),
    ],
    5: [
        (_ts(D2, 10, 30), "## Lane split (gatekeeper)\n\nSplit rationale.\n"),
        (_ts(D2, 10, 45), "## Re-cut (gatekeeper)\n\nRe-cut rationale.\n"),
        (_ts(D2, 11, 0), _PREMISE_FALSIFIED_D2),
    ],
    6: [
        (_ts(D1, 11, 0), "Just checking in, no update yet.\n"),
        (_ts(D1, 11, 15), "## Released in v1.2\n\nShipped without the gatekeeper suffix.\n"),
    ],
    7: [
        (_ts(BEFORE_SINCE, 9, 0), clarification_comment(1)),
        (_ts(D1, 9, 0), released_comment("single #7")),
    ],
    8: [
        (_ts(D1, 8, 15), clarification_comment(1)),
    ],
    9: [
        (_ts(D2, 12, 0), released_comment(_PACKAGE_LINE_9_10)),
    ],
    10: [
        (_ts(D2, 12, 0), released_comment(_PACKAGE_LINE_9_10)),
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


def test_clarification_block_in_daily_json(
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
        "released": 2,
        "released_without_asking": 1,
        "clarifications": 2,
        "questions": 3,
        "hard_clarifications": 0,
        "frames": 1,
        "frames_ac_rewritten": 1,
        "premises": 3,
        "not_proven": 1,
        "frames_without_block": 0,
        "lane_splits": 0,
        "re_cuts": 0,
        "premise_falsified": 0,
        "value": 0.5,
    }
    expected_day2 = {
        "released": 2,
        "released_without_asking": 0,
        "clarifications": 1,
        "questions": 4,
        "hard_clarifications": 1,
        "frames": 1,
        "frames_ac_rewritten": 0,
        "premises": 0,
        "not_proven": 2,
        "frames_without_block": 1,
        "lane_splits": 1,
        "re_cuts": 1,
        "premise_falsified": 1,
        "value": 0.0,
    }

    assert day1["totals"]["clarification"] == expected_day1
    assert day2["totals"]["clarification"] == expected_day2
    # per_repo is byte-for-byte the same as totals -- there is only one repo.
    assert day1["per_repo"]["acme/demo"]["clarification"] == expected_day1
    assert day2["per_repo"]["acme/demo"]["clarification"] == expected_day2

    # A rerun into a second --out is byte-identical (AGENTS.md: reproducible output).
    out_dir2 = workdir / "out2"
    cache_dir2 = workdir / "cache2"
    assert _run_collect(
        monkeypatch, workdir, out_dir2, cache_dir2, since="2024-03-04", until="2024-03-05"
    ) == 0
    assert read_tree(out_dir) == read_tree(out_dir2)


# ---------------------------------------------------------------------------
# R3: _merge_clarification sums across repos, recomputes value
# ---------------------------------------------------------------------------
#
# Expected RED reason: `ImportError` -- `_merge_clarification` does not exist yet in
# `ecosystem_statistics.collect`.
#
# The fixture is deliberately two-repo, so a naive `blocks[-1]` (last-write-wins)
# implementation would produce a different result than the real sum below -- mirroring
# `test_merge_regression_chains_sums_across_repos_not_last_write_wins`'s (#13) shape.


def test_merge_clarification_sums_across_repos_not_last_write_wins() -> None:
    from ecosystem_statistics.collect import _merge_clarification

    repo_a = {
        "released": 4,
        "released_without_asking": 1,
        "clarifications": 2,
        "questions": 5,
        "hard_clarifications": 1,
        "frames": 1,
        "frames_ac_rewritten": 1,
        "premises": 3,
        "not_proven": 1,
        "frames_without_block": 0,
        "lane_splits": 1,
        "re_cuts": 0,
        "premise_falsified": 0,
        "value": 0.25,
    }
    repo_b = {
        "released": 6,
        "released_without_asking": 2,
        "clarifications": 1,
        "questions": 1,
        "hard_clarifications": 0,
        "frames": 2,
        "frames_ac_rewritten": 0,
        "premises": 0,
        "not_proven": 3,
        "frames_without_block": 2,
        "lane_splits": 0,
        "re_cuts": 1,
        "premise_falsified": 2,
        "value": 0.3333,
    }

    merged = _merge_clarification([repo_a, repo_b])

    assert merged == {
        "released": 10,
        "released_without_asking": 3,
        "clarifications": 3,
        "questions": 6,
        "hard_clarifications": 1,
        "frames": 3,
        "frames_ac_rewritten": 1,
        "premises": 3,
        "not_proven": 4,
        "frames_without_block": 2,
        "lane_splits": 1,
        "re_cuts": 1,
        "premise_falsified": 2,
        "value": 0.3,
    }
    # A naive `blocks[-1]` (last-write-wins) copy would equal repo_b exactly.
    assert merged != repo_b


def test_merge_clarification_value_is_none_when_released_is_zero_everywhere() -> None:
    from ecosystem_statistics.collect import _merge_clarification

    zero_block = {
        "released": 0,
        "released_without_asking": 0,
        "clarifications": 0,
        "questions": 0,
        "hard_clarifications": 0,
        "frames": 0,
        "frames_ac_rewritten": 0,
        "premises": 0,
        "not_proven": 0,
        "frames_without_block": 0,
        "lane_splits": 0,
        "re_cuts": 0,
        "premise_falsified": 0,
        "value": None,
    }

    merged = _merge_clarification([zero_block, zero_block])

    assert merged["released"] == 0
    assert merged["value"] is None
