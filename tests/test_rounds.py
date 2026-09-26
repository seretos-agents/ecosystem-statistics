"""R2/R3 (plan #12) -- pure parser, session, lane and stats units for
`ecosystem_statistics.rounds` (brand new) and `ecosystem_statistics.escalations.parse_rounds`
(a new function added to the existing #11 module).

This module has no GitHub I/O and no wall clock: `daily_samples` turns a ticket's ordered
`(created_at, body)` comments into per-lane round-count samples for a UTC day, and
`summarize` pools those samples (across days or across repos) into the avg/median/p90
report shape ticket #12 wants.

Ground truth for the rounds-line grammar and the session/lane rules below is plan #12's
"Approach" section, itself grounded in the two emitters' `event_block.py` (see the plan's
"Premises verified"): every dev block renders all five dev gates
(`plan-critic`/`test-critic`/`review`/`ci`/`rebase`) as `gate=U/3(Nf,Ni)`, and every prose
block renders all five prose gates (`scenario-critic`/`evidence`/`review`/`ci`/`rebase`)
the same way.

Importing `ecosystem_statistics.rounds` is the deliberate RED for this file (plan #12 R2:
"Expected RED reason: ModuleNotFoundError: ecosystem_statistics.rounds, or ImportError:
parse_rounds") -- `rounds.py` is pure, brand-new, and not created until the implement
phase, and `escalations.parse_rounds` doesn't exist on the already-real `escalations`
module either. The `rounds` import comes first, so the module import failure is what
actually surfaces.
"""

from __future__ import annotations

from datetime import date

from conftest import adev_event_block

import ecosystem_statistics.rounds as rounds
from ecosystem_statistics.escalations import IssueHistory, parse_rounds

D1 = date(2024, 3, 4)


def _ts(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z"


def _dev_rounds(*, plan_critic="0/3(0f,0i)", test_critic="0/3(0f,0i)", review="0/3(0f,0i)",
                 ci="0/3(0f,0i)", rebase="0/3(0f,0i)") -> str:
    return (
        f"plan-critic={plan_critic} test-critic={test_critic} review={review} "
        f"ci={ci} rebase={rebase}"
    )


def _prose_rounds(*, scenario_critic="0/3(0f,0i)", evidence="0/3(0f,0i)", review="0/3(0f,0i)",
                   ci="0/3(0f,0i)", rebase="0/3(0f,0i)") -> str:
    return (
        f"scenario-critic={scenario_critic} evidence={evidence} review={review} "
        f"ci={ci} rebase={rebase}"
    )


def _terminal_block_without_rounds_line(event: str) -> str:
    """A hand-written terminal block that omits the `rounds:` line entirely -- the
    fixture generator (`conftest.adev_event_block`) always renders one, so this edge
    case (a session's terminal comment carrying no rounds snapshot of its own) has to
    be built by hand."""
    return (
        "<!-- adev:event v1\n"
        f"event: {event}\n"
        "package: 12\n"
        "attempt: 1\n"
        "generation: 1/1\n"
        "pr:\n"
        "ci_run:\n"
        "-->\n"
    )


# ---------------------------------------------------------------------------
# parse_rounds: the shared rounds-line parser (escalations.py)
# ---------------------------------------------------------------------------


def test_parse_rounds_returns_all_five_gates() -> None:
    line = _dev_rounds(plan_critic="2/3(1f,0i)", test_critic="1/3(0f,0i)",
                        review="4/3(2f,1i)", ci="1/3(0f,0i)", rebase="0/3(0f,0i)")

    gates = parse_rounds(line)

    assert set(gates.keys()) == {"plan-critic", "test-critic", "review", "ci", "rebase"}
    review_gate = gates["review"]
    assert review_gate.used == 4
    assert review_gate.soft == 3
    assert review_gate.f == 2
    assert review_gate.i == 1


# ---------------------------------------------------------------------------
# Session/lane rules, exercised through daily_samples + summarize -- daily_samples'
# per-day return value (`DaySamples`) is only ever fed straight into `summarize`
# below, never inspected field-by-field, since its internal shape belongs to
# `rounds.py`, not to this test.
# ---------------------------------------------------------------------------


def test_terminal_block_with_no_rounds_line_uses_the_earlier_snapshot() -> None:
    """A terminal event that itself carries no `rounds:` line (hand-written, not
    something either emitter actually produces, but the parser must not crash on it)
    falls back to the session's last snapshot -- the `review-verdict` block here."""
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block(
            "review-verdict", rounds=_dev_rounds(plan_critic="2/3(1f,0i)")
        )),
        (_ts(D1, 10, 0), _terminal_block_without_rounds_line("ci-green")),
    )
    issue = IssueHistory(number=1, closed_at=None, comments=comments)

    samples = rounds.daily_samples([issue], [D1])
    summary = rounds.summarize([samples[D1]])

    assert summary["lanes"]["dev"]["sessions"] == 1
    assert summary["lanes"]["dev"]["gates"]["plan-critic"]["used"] == {
        "avg": 2.0, "median": 2.0, "p90": 2,
    }


def test_snapshot_before_a_later_started_is_not_used() -> None:
    """`started` resets the session's snapshot memory. A restart with no new snapshot
    of its own, followed by a terminal event that also carries no `rounds:` line,
    therefore has nothing to sample -- it is skipped from the lane breakdown even
    though it still counts toward the ticket-level `sessions` (F2) below."""
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block(
            "review-verdict", rounds=_dev_rounds(plan_critic="2/3(1f,0i)")
        )),
        (_ts(D1, 10, 0), adev_event_block("started")),  # restart: forgets the snapshot above
        (_ts(D1, 11, 0), _terminal_block_without_rounds_line("ci-green")),
    )
    issue = IssueHistory(number=2, closed_at=None, comments=comments)

    samples = rounds.daily_samples([issue], [D1])
    summary = rounds.summarize([samples[D1]])

    # F2's ticket-level session count is anchored on the terminal event alone, and
    # does not require a snapshot: both `started` events (09:00 and 10:00) precede
    # the 11:00 terminal event.
    assert summary["sessions"] == {"tickets": 1, "started": 2, "per_ticket": 2.0}
    # But the lane/gate sample is skipped -- there is no snapshot for this session.
    assert summary["lanes"]["dev"]["sessions"] == 0
    assert summary["lanes"]["dev"]["gates"] == {}


def test_snapshot_with_unknown_gates_is_skipped() -> None:
    """Neither `plan-critic` nor `scenario-critic` appears in the snapshot, so the
    lane is unknown and the session is skipped -- not fabricated into either lane."""
    unknown_block = (
        "<!-- adev:event v1\n"
        "event: ci-green\n"
        "package: 12\n"
        "attempt: 1\n"
        "generation: 1/1\n"
        "rounds: foo=1/3(0f,0i) bar=2/3(1f,0i)\n"
        "pr:\n"
        "ci_run:\n"
        "-->\n"
    )
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 10, 0), unknown_block),
    )
    issue = IssueHistory(number=3, closed_at=None, comments=comments)

    samples = rounds.daily_samples([issue], [D1])
    summary = rounds.summarize([samples[D1]])

    # Still anchored on the terminal event for the ticket-level session count.
    assert summary["sessions"] == {"tickets": 1, "started": 1, "per_ticket": 1.0}
    assert summary["lanes"]["dev"]["sessions"] == 0
    assert summary["lanes"]["dev"]["gates"] == {}
    assert summary["lanes"]["prose"]["sessions"] == 0
    assert summary["lanes"]["prose"]["gates"] == {}


def test_used_counting_scheme_differs_by_lane() -> None:
    """Dev's `used` is the literal counter; prose's `used` is `f + i` (plan #12,
    Approach, F1) -- the same `=3/3(1f,1i)` snapshot means a different `used` value
    depending on which lane it belongs to."""
    dev_issue = IssueHistory(
        number=10,
        closed_at=None,
        comments=(
            (_ts(D1, 9, 0), adev_event_block("started")),
            (_ts(D1, 10, 0), adev_event_block(
                "ci-green", rounds=_dev_rounds(review="3/3(1f,1i)")
            )),
        ),
    )
    prose_issue = IssueHistory(
        number=11,
        closed_at=None,
        comments=(
            (_ts(D1, 9, 0), adev_event_block("started")),
            (_ts(D1, 10, 0), adev_event_block(
                "failed", rounds=_prose_rounds(scenario_critic="3/3(1f,1i)")
            )),
        ),
    )

    samples = rounds.daily_samples([dev_issue, prose_issue], [D1])
    summary = rounds.summarize([samples[D1]])

    assert summary["lanes"]["dev"]["counting"] == "used"
    assert summary["lanes"]["dev"]["gates"]["review"]["used"] == {
        "avg": 3.0, "median": 3.0, "p90": 3,
    }
    assert summary["lanes"]["prose"]["counting"] == "f+i"
    assert summary["lanes"]["prose"]["gates"]["scenario-critic"]["used"] == {
        "avg": 2.0, "median": 2.0, "p90": 2,
    }


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def test_p90_is_nearest_rank() -> None:
    assert rounds.p90(list(range(1, 11))) == 9


def test_median_averages_the_two_middle_values_for_even_n() -> None:
    assert rounds.median([1, 2, 3, 4]) == 2.5


# ---------------------------------------------------------------------------
# R3 -- summarize pools raw samples, it does not average per-repo summaries
# ---------------------------------------------------------------------------


def test_summarize_pools_not_averages() -> None:
    """Repo A's `plan-critic` used sample is `[1]`, repo B's is `[2, 6]`. Pooling the
    three raw samples together gives avg 3.0 / median 2 / p90 6 -- averaging the two
    repos' own averages (1.0 and 4.0) would instead give 2.5."""
    repo_a_issue = IssueHistory(
        number=20,
        closed_at=None,
        comments=(
            (_ts(D1, 9, 0), adev_event_block("started")),
            (_ts(D1, 10, 0), adev_event_block(
                "ci-green", rounds=_dev_rounds(plan_critic="1/3(0f,0i)")
            )),
        ),
    )
    repo_b_issue_1 = IssueHistory(
        number=21,
        closed_at=None,
        comments=(
            (_ts(D1, 9, 0), adev_event_block("started")),
            (_ts(D1, 10, 0), adev_event_block(
                "ci-green", rounds=_dev_rounds(plan_critic="2/3(0f,0i)")
            )),
        ),
    )
    repo_b_issue_2 = IssueHistory(
        number=22,
        closed_at=None,
        comments=(
            (_ts(D1, 9, 0), adev_event_block("started")),
            (_ts(D1, 10, 0), adev_event_block(
                "ci-green", rounds=_dev_rounds(plan_critic="6/3(0f,0i)")
            )),
        ),
    )

    samples_a = rounds.daily_samples([repo_a_issue], [D1])
    samples_b = rounds.daily_samples([repo_b_issue_1, repo_b_issue_2], [D1])

    pooled = rounds.summarize([samples_a[D1], samples_b[D1]])

    assert pooled["lanes"]["dev"]["gates"]["plan-critic"]["used"] == {
        "avg": 3.0, "median": 2, "p90": 6,
    }

    # Confirm it really is a pool, not last-write-wins or an average-of-averages:
    # repo A alone would give avg 1.0, repo B alone would give avg 4.0.
    repo_a_only = rounds.summarize([samples_a[D1]])
    repo_b_only = rounds.summarize([samples_b[D1]])
    assert repo_a_only["lanes"]["dev"]["gates"]["plan-critic"]["used"]["avg"] == 1.0
    assert repo_b_only["lanes"]["dev"]["gates"]["plan-critic"]["used"]["avg"] == 4.0
