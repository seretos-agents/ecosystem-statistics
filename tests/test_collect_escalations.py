"""R1 (plan #11) -- the `escalations` block in `daily/<day>.json`, end to end through
`collect` over a `MockTransport` standing in for GitHub's REST API (never a real
network call -- AGENTS.md).

Fixture (verbatim from the plan): `--since 2024-03-04(D1) --until 2024-03-05(D2)`.
`/pulls` returns `[]`. `/issues` also serves a PR item, #8 (created D3), and #9 (closed
before D1) -- their comments must never be requested. #1-#4, #6, #7 and #10 each have
`started` on D1; #5 has only a plain (non-machine-block) comment and never counts.

    #1: blocked D1, triage heading D1.
    #2: failed D2, then a free-text `Escalated:` D2.
    #3: `review-verdict` D1 (rebase=0/3(0f,0i)), then a fenced `blocked` D2
        (rebase=1/3(1f,0i)), then one D2 comment with an escaped ato `escalated`
        block plus an `Escalated:` line.
    #4: blocked D1, ci-green D1, `event: frobnicate`.
    #6: blocked D1 23:50, triage answer D2 00:10.
    #7: blocked D1 23:00, `Escalated:` D2 01:00.
    #10: `tests-green` D1 (rebase=1/3(1f,0i)), then `blocked` D2 (same counter), then
        a triage answer D3.

Expected RED reason: `KeyError: 'escalations'` -- `collect.py` does not produce this
key yet, so the first assertion below fails for exactly that reason.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, adev_event_block, ato_event_block, escaped, fenced, read_tree
from ecosystem_statistics.collect import _merge_escalations

D1 = date(2024, 3, 4)
D2 = date(2024, 3, 5)
D3 = date(2024, 3, 6)


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
# The fixture: ten issues (#1-#10), only seven of which ever contribute.
# ---------------------------------------------------------------------------

_ISSUES_META = [
    {"number": 1, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 2, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 3, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 4, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 5, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 6, "created_at": _ts(D1, 8, 0), "closed_at": None},
    {"number": 7, "created_at": _ts(D1, 8, 0), "closed_at": None},
    # A PR, not an issue -- `list_issues` must skip it regardless of its date.
    {
        "number": 8,
        "created_at": _ts(D3, 8, 0),
        "closed_at": None,
        "pull_request": {"url": "https://api.github.com/repos/acme/demo/pulls/8"},
    },
    # Closed before --since (2024-03-01, before D1) -- history collect.py must not
    # fetch comments for.
    {"number": 9, "created_at": "2024-02-01T08:00:00Z", "closed_at": "2024-03-01T08:00:00Z"},
    {"number": 10, "created_at": _ts(D1, 8, 0), "closed_at": None},
]

_COMMENTS_BY_NUMBER: dict[int, list[tuple[str, str]]] = {
    1: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 5), adev_event_block("blocked")),
        (_ts(D1, 9, 10), "## Blocked triage (run)\n\nRe-ran; the flake cleared on retry.\n"),
    ],
    2: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D2, 10, 0), adev_event_block("failed")),
        (_ts(D2, 10, 30), "Escalated: CI keeps failing after three retries.\n"),
    ],
    3: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 10, 0), adev_event_block("review-verdict", rebase="0/3(0f,0i)")),
        (_ts(D2, 11, 0), fenced(adev_event_block("blocked", rebase="1/3(1f,0i)"))),
        (
            _ts(D2, 11, 30),
            escaped(ato_event_block("escalated", reason="rebase-decision"))
            + "\nEscalated: a human should pick a resolution strategy.\n",
        ),
    ],
    4: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block("blocked")),
        (_ts(D1, 9, 45), adev_event_block("ci-green")),
        (_ts(D1, 10, 0), adev_event_block("frobnicate")),
    ],
    5: [
        (_ts(D1, 9, 0), "Thanks for looking into this, no update yet.\n"),
    ],
    6: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 23, 50), adev_event_block("blocked")),
        (_ts(D2, 0, 10), "## Blocked triage (run)\n\nAnswered from a sibling ticket's precedent.\n"),
    ],
    7: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 23, 0), adev_event_block("blocked")),
        (_ts(D2, 1, 0), "Escalated: nobody answered triage in time.\n"),
    ],
    10: [
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 12, 0), adev_event_block("tests-green", rebase="1/3(1f,0i)")),
        (_ts(D2, 12, 0), adev_event_block("blocked", rebase="1/3(1f,0i)")),
        (
            _ts(D3, 9, 0),
            "## Blocked triage (run)\n\nAnswered once the window widens far enough to see it.\n",
        ),
    ],
}

# #8 (a PR) and #9 (closed before --since) must never have their comments requested --
# there is deliberately no entry for them in `_COMMENTS_BY_NUMBER`.
_NEVER_FETCHED = {8, 9}


def _make_client_factory():
    def make_client(token: str | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/issues"):
                assert request.url.params.get("state") == "all"
                return httpx.Response(200, json=_ISSUES_META)
            if "/issues/" in path and path.endswith("/comments"):
                number = int(path.rsplit("/issues/", 1)[1].split("/", 1)[0])
                assert number not in _NEVER_FETCHED, (
                    f"issue #{number}'s comments must never be requested (it is a PR "
                    "item or was closed before --since)"
                )
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


def test_escalations_block_in_daily_json(
    tmp_path: Path, synthetic_repo: SyntheticRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = synthetic_repo
    origin.commit_file(
        "a.py", "a\n", "init", when=datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    )

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", origin.clone_url())
    monkeypatch.chdir(workdir)

    from ecosystem_statistics.__main__ import main
    import ecosystem_statistics.github as github

    monkeypatch.setattr(github, "make_client", _make_client_factory())

    out_dir = workdir / "out"
    cache_dir = workdir / "cache"
    assert main(
        [
            "collect",
            "--since", "2024-03-04",
            "--until", "2024-03-05",
            "--out", str(out_dir),
            "--cache-dir", str(cache_dir),
        ]
    ) == 0

    day1 = _read_day(out_dir, D1)
    day2 = _read_day(out_dir, D2)

    expected_day1 = {
        "in_progress": 7,
        "escalated": 0,
        "auto_answered": 1,
        "value": 0.0,
        "by_reason": {},
    }
    expected_day2 = {
        "in_progress": 6,
        "escalated": 4,
        "auto_answered": 1,
        "value": 0.6667,
        "by_reason": {"blocked": 2, "failed": 1, "rebase-conflict-decision": 1},
    }

    assert day1["totals"]["escalations"] == expected_day1
    assert day2["totals"]["escalations"] == expected_day2
    assert day1["per_repo"]["acme/demo"]["escalations"] == expected_day1
    assert day2["per_repo"]["acme/demo"]["escalations"] == expected_day2

    # Plan-critic note 3's invariant: escalated (and auto_answered) tickets are always
    # a subset of in_progress tickets, so the rate can never exceed 1 by construction.
    for esc in (day1["totals"]["escalations"], day2["totals"]["escalations"]):
        assert esc["escalated"] <= esc["in_progress"]
        assert esc["auto_answered"] <= esc["in_progress"]
        assert esc["value"] is None or 0.0 <= esc["value"] <= 1.0

    # A rerun into a second --out is byte-identical (AGENTS.md: reproducible output).
    out_dir2 = workdir / "out2"
    cache_dir2 = workdir / "cache2"
    assert main(
        [
            "collect",
            "--since", "2024-03-04",
            "--until", "2024-03-05",
            "--out", str(out_dir2),
            "--cache-dir", str(cache_dir2),
        ]
    ) == 0
    assert read_tree(out_dir) == read_tree(out_dir2)

    # --since D2 --until D2 alone writes the same D2 file byte-for-byte as the wider
    # D1..D2 run above.
    out_d2_only = workdir / "out_d2_only"
    cache_d2_only = workdir / "cache_d2_only"
    assert main(
        [
            "collect",
            "--since", "2024-03-05",
            "--until", "2024-03-05",
            "--out", str(out_d2_only),
            "--cache-dir", str(cache_d2_only),
        ]
    ) == 0
    assert (out_d2_only / "daily" / "2024-03-05.json").read_bytes() == (
        out_dir / "daily" / "2024-03-05.json"
    ).read_bytes()

    # Widening --until to D3 makes #10's D3 triage answer visible: it flips from a
    # window-boundary escalation fallback on D2 to a genuine auto-answer on D3 (plan
    # note 2 -- "a wider --until may flip an older day").
    out_wide = workdir / "out_wide"
    cache_wide = workdir / "cache_wide"
    assert main(
        [
            "collect",
            "--since", "2024-03-04",
            "--until", "2024-03-06",
            "--out", str(out_wide),
            "--cache-dir", str(cache_wide),
        ]
    ) == 0
    day2_wide = _read_day(out_wide, D2)
    day3_wide = _read_day(out_wide, D3)

    assert day2_wide["totals"]["escalations"] == {
        "in_progress": 6,
        "escalated": 3,
        "auto_answered": 1,
        "value": 0.5,
        "by_reason": {"blocked": 1, "failed": 1, "rebase-conflict-decision": 1},
    }
    assert day3_wide["totals"]["escalations"] == {
        "in_progress": 3,
        "escalated": 0,
        "auto_answered": 1,
        "value": 0.0,
        "by_reason": {},
    }


# ---------------------------------------------------------------------------
# Review finding R1: `_merge_escalations` must do a real per-field sum and
# `by_reason` dict-merge across repos, then recompute `value` -- not
# last-write-wins. The end-to-end fixture above is deliberately single-repo (per
# the plan's exact numbers), so it cannot itself distinguish a real sum from a
# copy of the last block; this direct unit test supplies two blocks whose
# `by_reason` keys overlap on `blocked` (1+1=2) and each carry a reason the
# other lacks (`failed` only in the second, `rebase-conflict-decision` only in
# the first), so a naive `blocks[-1]` implementation would produce different
# `in_progress`/`escalated`/`auto_answered`/`by_reason`/`value` than the real
# sum below.
# ---------------------------------------------------------------------------


def test_merge_escalations_sums_across_repos_not_last_write_wins() -> None:
    repo_a = {
        "in_progress": 3,
        "escalated": 1,
        "auto_answered": 1,
        "value": 0.3333,
        "by_reason": {"blocked": 1, "rebase-conflict-decision": 1},
    }
    repo_b = {
        "in_progress": 5,
        "escalated": 2,
        "auto_answered": 0,
        "value": 0.4,
        "by_reason": {"blocked": 1, "failed": 1},
    }

    merged = _merge_escalations([repo_a, repo_b])

    assert merged == {
        "in_progress": 8,
        "escalated": 3,
        "auto_answered": 1,
        "value": 0.375,
        "by_reason": {"blocked": 2, "rebase-conflict-decision": 1, "failed": 1},
    }
    # A naive `blocks[-1]` (last-write-wins) copy would equal repo_b exactly --
    # confirm the real merge differs from it on every summed field.
    assert merged != repo_b
