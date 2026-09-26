"""R2 (plan #11) -- pure parser and per-ticket classification units for
`ecosystem_statistics.escalations`.

This module has no GitHub I/O and no wall clock: it turns one ticket's ordered
`(created_at, body)` comments (plus the issue's `closed_at`) into a verdict --
whether, and on what day, an attempt was auto-answered or escalated, and why.

Ground truth for the machine-block formats these tests build come from the two
plugins that actually render them (not guessed): `agent-autonomous-developer`'s
`process-developer` SKILL.md ("What the renderer prints for `--event
review-verdict ...`") for `adev:event v1`, and `agent-ticket-orchestrator`'s
`scripts/run/ato-event.py` for `ato:event v1`. `conftest.adev_event_block` /
`ato_event_block` reproduce those renderers' exact output.

Covers plan #11's R2 behaviours plus the four plan-critic notes forwarded to the
developer for resolution against real code:

- note 4: the free-text `Escalated:` matcher must not fire on a quoted line or on
  prefixed/negated prose.
- note 1: a comment carrying both a triage-answer heading and an explicit
  `Escalated:` line must not double-count as both auto_answered and
  escalated(other) -- the explicit signal wins outright, keeping the original
  pending reason.
- note 2 and note 3 are exercised at the integration level in
  `test_collect_escalations.py`, where the multi-day, multi-ticket fixture makes
  the window-boundary and in_progress-invariant behaviour observable; the
  `daily_breakdown` calls here also touch note 3's invariant per single-ticket
  case.

R3 (`list_issue_comments` pagination) is grouped here per plan #11's test
strategy, even though it exercises `ecosystem_statistics.github` rather than
`escalations` -- the plan's Affected files section names this file, not a new
`test_github_*` file, for that edge case.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from conftest import adev_event_block, ato_event_block, escaped, fenced

# Importing a module that does not exist yet is the deliberate RED for every test in
# this file (plan #11 R2: "Expected RED reason: ModuleNotFoundError") -- escalations.py
# is pure, brand-new, and not created until the implement phase. `import
# ecosystem_statistics.escalations` (rather than `from ecosystem_statistics import
# escalations`) is deliberate: the latter's absent-submodule case raises `ImportError:
# cannot import name ...`, not literally `ModuleNotFoundError`.
import ecosystem_statistics.escalations as escalations
from ecosystem_statistics import github


def _ts(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z"


D1 = date(2024, 3, 4)
D2 = date(2024, 3, 5)
D3 = date(2024, 3, 6)


# ---------------------------------------------------------------------------
# extract_blocks: raw / fenced / escaped forms, unknown events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(lambda block: block, id="raw"),
        pytest.param(fenced, id="fenced"),
        pytest.param(escaped, id="escaped"),
    ],
)
def test_extract_blocks_finds_adev_block_in_every_physical_form(wrap) -> None:
    body = "Some preamble.\n\n" + wrap(adev_event_block("blocked")) + "\nTrailing text.\n"

    blocks = escalations.extract_blocks(body)

    assert len(blocks) == 1
    assert blocks[0]["kind"] == "adev"
    assert blocks[0]["event"] == "blocked"


def test_extract_blocks_does_not_raise_on_unknown_event() -> None:
    body = adev_event_block("frobnicate")

    blocks = escalations.extract_blocks(body)

    assert blocks[0]["event"] == "frobnicate"  # parsed, not validated against a vocabulary


def test_classify_issue_ignores_unknown_event_block() -> None:
    """A trailing unknown-event block (plan #4's fixture: `event: frobnicate` after
    ci-green) must not raise, and must not resurrect a pending that ci-green already
    cleared."""
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block("blocked")),
        (_ts(D1, 9, 45), adev_event_block("ci-green")),
        (_ts(D1, 10, 0), adev_event_block("frobnicate")),
    )
    issue = escalations.IssueHistory(number=4, closed_at=None, comments=comments)

    outcome = escalations.classify_issue(issue, until=D1)

    assert outcome is not None
    assert outcome.start_day == D1
    assert outcome.resolution is None  # cleared by ci-green, never counted


# ---------------------------------------------------------------------------
# A ticket with no terminal event and no escalation adds 0
# ---------------------------------------------------------------------------


def test_ticket_with_only_started_contributes_zero_escalations() -> None:
    comments = ((_ts(D1, 9, 0), adev_event_block("started")),)
    issue = escalations.IssueHistory(number=99, closed_at=None, comments=comments)

    breakdown = escalations.daily_breakdown([issue], [D1])

    assert breakdown[D1] == {
        "in_progress": 1,
        "escalated": 0,
        "auto_answered": 0,
        "value": 0.0,
        "by_reason": {},
    }


# ---------------------------------------------------------------------------
# rebase-f delta: unchanged -> blocked, incremented -> rebase-conflict-decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prev_rebase, cur_rebase, expected_reason",
    [
        pytest.param("0/3(0f,0i)", "0/3(0f,0i)", "blocked", id="unchanged-f"),
        pytest.param("0/3(0f,0i)", "1/3(1f,0i)", "rebase-conflict-decision", id="incremented-f"),
        pytest.param(
            "1/3(1f,0i)", "1/3(1f,0i)", "blocked", id="inherited-cumulative-f-does-not-match"
        ),
    ],
)
def test_blocked_reason_depends_on_rebase_f_delta(prev_rebase, cur_rebase, expected_reason) -> None:
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block("review-verdict", rebase=prev_rebase)),
        (_ts(D1, 10, 0), adev_event_block("blocked", rebase=cur_rebase)),
    )
    issue = escalations.IssueHistory(number=3, closed_at=None, comments=comments)

    outcome = escalations.classify_issue(issue, until=D1)

    assert outcome.resolution is not None
    assert outcome.resolution.reason == expected_reason


# ---------------------------------------------------------------------------
# blocked -> closed_at is escalated on the blocked day (closed_at does not clear it)
# ---------------------------------------------------------------------------


def test_blocked_then_closed_is_escalated_on_the_blocked_day() -> None:
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block("blocked")),
    )
    issue = escalations.IssueHistory(number=44, closed_at=_ts(D2, 8, 0), comments=comments)

    outcome = escalations.classify_issue(issue, until=D2)

    assert outcome.resolution == escalations.Resolution(day=D1, kind="escalated", reason="blocked")


# ---------------------------------------------------------------------------
# An Escalated: with nothing pending gives "other"
# ---------------------------------------------------------------------------


def test_explicit_escalation_with_nothing_pending_gives_other() -> None:
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 12, 0), "Escalated: a human decided to step in unprompted.\n"),
    )
    issue = escalations.IssueHistory(number=55, closed_at=None, comments=comments)

    outcome = escalations.classify_issue(issue, until=D1)

    assert outcome.resolution == escalations.Resolution(day=D1, kind="escalated", reason="other")


# ---------------------------------------------------------------------------
# Note 4: the Escalated: line matcher must not fire on a quote or prefixed/negated prose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected",
    [
        pytest.param("Escalated: needs a human.\n", True, id="plain"),
        pytest.param("   Escalated: needs a human.\n", True, id="stripped-leading-whitespace"),
        pytest.param("Escalated: needs a human.   \n", True, id="stripped-trailing-whitespace"),
        pytest.param(
            "Something else first.\nEscalated: needs a human.\n", True, id="own-line-later-in-comment"
        ),
        pytest.param("> Escalated: needs a human.\n", False, id="blockquote"),
        pytest.param("Not Escalated: false alarm.\n", False, id="negated-prefix"),
        pytest.param("### Escalated: no\n", False, id="heading-prefixed"),
        pytest.param("**Escalated:** needs a human.\n", False, id="markdown-bold-prefixed"),
    ],
)
def test_explicit_escalation_line_matching_is_strict(body, expected) -> None:
    assert escalations.has_explicit_escalation(body) is expected


# ---------------------------------------------------------------------------
# At most one explicit escalation per comment (an ato block + a free-text line together)
# ---------------------------------------------------------------------------


def test_ato_escalated_block_and_freetext_line_in_one_comment_count_once() -> None:
    combined = escaped(ato_event_block("escalated", reason="rebase-decision")) + (
        "\nEscalated: also flagged by a human.\n"
    )
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block("blocked")),
        (_ts(D1, 10, 0), combined),
    )
    issue = escalations.IssueHistory(number=77, closed_at=None, comments=comments)

    breakdown = escalations.daily_breakdown([issue], [D1])

    assert breakdown[D1]["escalated"] == 1
    assert breakdown[D1]["by_reason"] == {"blocked": 1}


def test_ato_triage_answered_block_counts_as_triage_answer() -> None:
    assert escalations.has_triage_answer(ato_event_block("triage-answered")) is True


def test_ato_escalated_block_alone_counts_as_explicit_escalation() -> None:
    assert escalations.has_explicit_escalation(ato_event_block("escalated", reason="blocked")) is True


# ---------------------------------------------------------------------------
# Note 1: explicit Escalated: always wins over a triage answer in the same comment
# ---------------------------------------------------------------------------


def test_explicit_escalation_overrides_triage_answer_in_same_comment() -> None:
    """Plan-critic note 1: a comment carrying both a triage-answer heading and an
    explicit `Escalated:` line must not double-count as both auto_answered and
    escalated(other). Resolution: the explicit human/orchestrator signal always wins
    for that attempt, and the resolution keeps the *original pending reason*
    (`blocked` here), not `other` -- `other` is reserved for an escalation with
    nothing pending at all (see the test above)."""
    combined = (
        "## Blocked triage (run)\n\n"
        "Re-ran, but a human should still look at this.\n\n"
        "Escalated: needs a second opinion.\n"
    )
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block("blocked")),
        (_ts(D2, 8, 0), combined),
    )
    issue = escalations.IssueHistory(number=66, closed_at=None, comments=comments)

    outcome = escalations.classify_issue(issue, until=D2)

    assert outcome.resolution == escalations.Resolution(day=D2, kind="escalated", reason="blocked")
    breakdown = escalations.daily_breakdown([issue], [D1, D2])
    assert breakdown[D2]["auto_answered"] == 0
    assert breakdown[D2]["escalated"] == 1


def test_triage_answer_alone_is_auto_answered_and_does_not_end_the_attempt() -> None:
    """The mirror case of the override above -- a triage answer with no `Escalated:`
    line anywhere resolves as auto_answered, and (per plan #11's per-day rule) does
    *not* end the attempt: the ticket stays in_progress on a later day even though it
    was never blocked/failed/escalated again."""
    comments = (
        (_ts(D1, 9, 0), adev_event_block("started")),
        (_ts(D1, 9, 30), adev_event_block("blocked")),
        (_ts(D1, 9, 40), "## Blocked triage (run)\n\nAnswered from precedent.\n"),
    )
    issue = escalations.IssueHistory(number=1, closed_at=None, comments=comments)

    outcome = escalations.classify_issue(issue, until=D2)

    assert outcome.resolution == escalations.Resolution(day=D1, kind="auto_answered", reason=None)
    breakdown = escalations.daily_breakdown([issue], [D1, D2])
    assert breakdown[D1]["auto_answered"] == 1
    # Still in progress on D2 -- ci-green/escalation/closed_at end an attempt;
    # auto-answering a triage question does not.
    assert breakdown[D2]["in_progress"] == 1
    assert breakdown[D2]["auto_answered"] == 0
    assert breakdown[D2]["escalated"] == 0


# ---------------------------------------------------------------------------
# Note 3: escalation rate can never exceed 1 by construction
# ---------------------------------------------------------------------------


def test_escalation_rate_invariant_holds_for_a_mixed_day() -> None:
    """escalated is always a subset of in_progress (an escalated ticket is, by
    definition, in progress that day), so `value` can never exceed 1 regardless of how
    many tickets escalate on the same day."""
    escalated_issue = escalations.IssueHistory(
        number=1,
        closed_at=None,
        comments=(
            (_ts(D1, 9, 0), adev_event_block("started")),
            (_ts(D1, 9, 30), adev_event_block("blocked")),
        ),
    )
    still_open_issue = escalations.IssueHistory(
        number=2, closed_at=None, comments=((_ts(D1, 9, 0), adev_event_block("started")),)
    )

    breakdown = escalations.daily_breakdown([escalated_issue, still_open_issue], [D1])

    day = breakdown[D1]
    assert day["escalated"] <= day["in_progress"]
    assert day["value"] is not None and 0.0 <= day["value"] <= 1.0
    assert day == {
        "in_progress": 2,
        "escalated": 1,
        "auto_answered": 0,
        "value": 0.5,
        "by_reason": {"blocked": 1},
    }


# ---------------------------------------------------------------------------
# R3 edge-case coverage (grouped here per plan #11's test strategy): pagination
# ---------------------------------------------------------------------------


def test_list_issue_comments_follows_link_next() -> None:
    page1 = [{"created_at": _ts(D1, 9, 0), "body": "first"}]
    page2 = [{"created_at": _ts(D1, 10, 0), "body": "second"}]
    next_url = "https://api.github.com/repos/acme/demo/issues/1/comments?page=2"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200, json=page1, headers={"Link": f'<{next_url}>; rel="next"'}
            )
        return httpx.Response(200, json=page2)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    comments = github.list_issue_comments(client, "acme", "demo", 1)

    assert len(calls) == 2  # the second page was actually fetched, not just linked
    assert [body for _created_at, body in comments] == ["first", "second"]
