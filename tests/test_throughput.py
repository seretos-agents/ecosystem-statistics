"""Additional edge-case coverage (plan #14) for `ecosystem_statistics.throughput`'s pure
`daily_samples`/`summarize` pair, built directly from `escalations.IssueHistory` (no
GitHub I/O, no `collect` CLI -- mirroring `test_rounds.py`'s (#12) and
`test_regression_chains.py`'s (#13) structure).

Importing `ecosystem_statistics.throughput` is the deliberate RED for this file (mirroring
plan #12's `test_rounds.py`: "Importing ecosystem_statistics.rounds is the deliberate RED
for this file") -- `throughput.py` is pure, brand-new, and not created until the implement
phase. Its import comes first, so `ModuleNotFoundError: ecosystem_statistics.throughput`
is what actually surfaces, even though this file also relies on `IssueHistory` gaining
`created_at`/`state_reason`/`title` fields that don't exist on today's `escalations.py`
either -- neither change lands before the implement phase.

Each test below isolates exactly one of plan #14's worked edge cases (round 3, "Test /
verification strategy", "Additional edge-case coverage"), not guessed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from conftest import adev_event_block, escaped, fenced

import ecosystem_statistics.throughput as throughput
from ecosystem_statistics.escalations import IssueHistory

D = date(2024, 3, 5)
D1 = date(2024, 3, 4)
D2 = date(2024, 3, 5)


def _ts(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z"


def _released(package_line: str) -> str:
    return f"## Released (gatekeeper)\n\nPackage: {package_line}\nChecked: n/a.\n"


def _summarize_days(issues: list[IssueHistory], days: list[date], merged_prs: dict) -> dict:
    per_day = throughput.daily_samples(issues, days, merged_prs)
    return throughput.summarize([per_day[day] for day in days])


# ---------------------------------------------------------------------------
# Pipeline classification: "pipeline" means "ever had a ci-green", independent of
# ordering against closed_at and independent of the close reason.
# ---------------------------------------------------------------------------


def test_ci_green_after_closed_at_still_counts_as_pipeline() -> None:
    issue = IssueHistory(
        number=10,
        created_at=_ts(D, 0, 0),
        closed_at=_ts(D, 10, 0),
        state_reason="completed",
        comments=((_ts(D, 14, 0), adev_event_block("ci-green")),),
    )

    result = _summarize_days([issue], [D], {})

    assert result["closed"]["completed"] == 1
    assert result["closed"]["pipeline"] == 1
    assert result["closed"]["manual"] == 0


def test_not_planned_close_with_ci_green_counts_as_pipeline() -> None:
    """The ticket's "geschlossen ohne je ein ci-green" definition sets no restriction on
    the close reason -- a not_planned close with a ci-green is still pipeline, not
    manual (plan #14 round 3's widened pipeline/manual split)."""
    issue = IssueHistory(
        number=11,
        created_at=_ts(D, 0, 0),
        closed_at=_ts(D, 10, 0),
        state_reason="not_planned",
        comments=((_ts(D, 5, 0), adev_event_block("ci-green")),),
    )

    result = _summarize_days([issue], [D], {})

    assert result["closed"]["not_planned"] == 1
    assert result["closed"]["pipeline"] == 1
    assert result["closed"]["manual"] == 0


# ---------------------------------------------------------------------------
# ci_green_to_done fallback: merged PR, else closed_at, else no sample.
# ---------------------------------------------------------------------------


def test_ci_green_to_done_falls_back_to_closed_at_when_pr_not_in_merged_prs() -> None:
    issue = IssueHistory(
        number=12,
        created_at=_ts(D, 0, 0),
        closed_at=_ts(D, 12, 0),
        state_reason="completed",
        comments=((_ts(D, 8, 0), adev_event_block("ci-green", pr="99")),),
    )

    result = _summarize_days([issue], [D], {})

    assert result["stages"]["ci_green_to_done"] == {"n": 1, "median": 4.0, "p90": 4.0}


def test_ci_green_to_done_has_no_sample_while_ticket_is_still_open() -> None:
    issue = IssueHistory(
        number=13,
        created_at=_ts(D, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D, 8, 0), adev_event_block("ci-green", pr="99")),),
    )

    result = _summarize_days([issue], [D], {})

    assert result["stages"]["ci_green_to_done"]["n"] == 0


def test_ci_green_to_done_falls_back_to_closed_at_when_pr_is_empty() -> None:
    issue = IssueHistory(
        number=14,
        created_at=_ts(D, 0, 0),
        closed_at=_ts(D, 9, 30),
        state_reason="completed",
        comments=((_ts(D, 8, 0), adev_event_block("ci-green")),),
    )

    result = _summarize_days([issue], [D], {})

    assert result["stages"]["ci_green_to_done"] == {"n": 1, "median": 1.5, "p90": 1.5}


# ---------------------------------------------------------------------------
# Parsing edge cases.
# ---------------------------------------------------------------------------


def test_plain_released_without_gatekeeper_suffix_is_not_counted() -> None:
    issue = IssueHistory(
        number=15,
        created_at=_ts(D, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D, 5, 0), "## Released\n\nShipped, no gatekeeper heading here.\n"),),
    )

    result = _summarize_days([issue], [D], {})

    assert result["events"]["released"] == 0
    assert result["stages"]["created_to_released"]["n"] == 0
    assert result["packages"]["total"] == 0


def test_started_without_earlier_released_still_counts_as_event_but_gives_no_sample() -> None:
    issue = IssueHistory(
        number=16,
        created_at=_ts(D, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D, 3, 0), adev_event_block("started")),),
    )

    result = _summarize_days([issue], [D], {})

    assert result["events"]["started"] == 1
    assert result["stages"]["released_to_started"]["n"] == 0


def test_fenced_and_escaped_pr_opened_are_both_recognised() -> None:
    fenced_issue = IssueHistory(
        number=17,
        created_at=_ts(D, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=(
            (_ts(D, 1, 0), adev_event_block("started")),
            (_ts(D, 2, 0), fenced(adev_event_block("pr-opened"))),
        ),
    )
    escaped_issue = IssueHistory(
        number=18,
        created_at=_ts(D, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=(
            (_ts(D, 1, 0), adev_event_block("started")),
            (_ts(D, 2, 0), escaped(adev_event_block("pr-opened"))),
        ),
    )

    result = _summarize_days([fenced_issue, escaped_issue], [D], {})

    assert result["stages"]["started_to_pr_opened"] == {"n": 2, "median": 1.0, "p90": 1.0}


def test_package_line_parses_with_en_dash_and_with_hyphen() -> None:
    en_dash_issue = IssueHistory(
        number=19,
        created_at=_ts(D, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D, 0, 0), _released("single #19 – single")),),
    )
    hyphen_issue = IssueHistory(
        number=20,
        created_at=_ts(D, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D, 0, 0), _released("single #20 - single")),),
    )

    result = _summarize_days([en_dash_issue, hyphen_issue], [D], {})

    assert result["packages"]["total"] == 2
    assert result["packages"]["by_reason"] == {"single": 2}


# ---------------------------------------------------------------------------
# summarize: pooling across days, not just within one day.
# ---------------------------------------------------------------------------


def test_summarize_pools_samples_across_days_for_median() -> None:
    """Ticket #21's cycle gives a 1h sample on D1; #22 and #23 give 3h and 5h samples on
    D2 -- summarize([day1, day2]) must pool all three into one median (3.0), not compute
    a median per day and merge those (plan #14: "summarize of ([1h], [3h, 5h]) gives
    median 3.0")."""
    one_hour = IssueHistory(
        number=21,
        created_at=_ts(D1, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D1, 1, 0), _released("single #21 — single")),),
    )
    three_hours = IssueHistory(
        number=22,
        created_at=_ts(D2, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D2, 3, 0), _released("single #22 — single")),),
    )
    five_hours = IssueHistory(
        number=23,
        created_at=_ts(D2, 0, 0),
        closed_at=None,
        state_reason=None,
        comments=((_ts(D2, 5, 0), _released("single #23 — single")),),
    )

    per_day = throughput.daily_samples([one_hour, three_hours, five_hours], [D1, D2], {})
    result = throughput.summarize([per_day[D1], per_day[D2]])

    assert result["stages"]["created_to_released"]["n"] == 3
    assert result["stages"]["created_to_released"]["median"] == 3.0
