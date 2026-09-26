"""Pure per-ticket escalation classification (plan #11).

No GitHub I/O and no wall clock: this module turns one ticket's ordered
`(created_at, body)` comments (plus the issue's `closed_at`) into a verdict --
whether, and on what UTC day, an attempt was auto-answered or escalated, and why --
and rolls per-ticket verdicts up into a per-day `escalations` breakdown.

Escalation is defined (ticket #11) as either:
  - an explicit `Escalated: ...` free-text line (or an `ato:event v1 escalated` block),
    or
  - a terminal `adev:event v1` `blocked`/`failed` event with no later `ci-green` and no
    later automatic triage answer (`## Blocked triage (run)`, or an `ato:event v1
    triage-answered` block).

Verdicts use the ticket's full history up to the end of `--until` -- there is no
per-day snapshot (plan #11's "Approach"). A `blocked`/`failed` event ("pending") is
resolved by the first of, in comment-time order:
  1. an explicit escalation in the same or a later comment -- wins outright over a
     triage answer in the *same* comment (plan-critic note 1) and keeps the
     *original pending reason*, never `other`.
  2. a triage answer (with no escalation in that comment) -- `auto_answered`, and,
     per plan #11, does *not* end the attempt (the ticket can still be in_progress on
     a later day).
  3. `ci-green` -- clears the pending event without counting it.
  4. end of history (nothing above happened before `until`) -- `escalated` on the
     pending event's own day, with the pending reason. `closed_at` does not clear a
     still-pending event.

An explicit escalation with nothing pending counts once, with reason `other`.

A `blocked` event's reason is `rebase-conflict-decision` only when its
`rebase=<u>/3(<f>f,...)` counter is *greater* than the `f` of the ticket's
immediately preceding `adev` block (a missing prior block counts as `f=0`); a later
unrelated `blocked` that inherits the same cumulative `f` does not match, and is
plain `blocked`.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

_BLOCK_RE = re.compile(r"<!--\s*(adev|ato):event\s+v1\b(.*?)-->", re.DOTALL)
_REBASE_F_RE = re.compile(r"rebase=\d+/\d+\((\d+)f")

_TRIAGE_HEADING = "## Blocked triage (run)"
_ESCALATED_PREFIX = "Escalated:"


@dataclass(frozen=True)
class IssueHistory:
    """One ticket's full comment history, as fetched via `github.list_issue_comments`
    (already filtered by `collect.py` to drop comments after the end of `--until`)."""

    number: int
    closed_at: str | None
    comments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Resolution:
    day: date
    kind: str  # "escalated" or "auto_answered"
    reason: str | None  # None for auto_answered; "blocked"/"failed"/"rebase-conflict-decision"/"other" for escalated


@dataclass(frozen=True)
class Outcome:
    start_day: date
    resolution: Resolution | None
    attempt_end_day: date | None  # None => the attempt is still open (unbounded)


def _day(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).date()


def extract_blocks(body: str) -> list[dict]:
    """Find every `adev:event v1` / `ato:event v1` machine block in `body`, in any of
    the three physical forms the parser must tolerate: raw HTML comment, inside a
    ```-fence, or HTML-escaped. Returns one dict per block, `{"kind": "adev"|"ato",
    ...}` plus every `key: value` line inside it (unknown event names are parsed, not
    validated against a vocabulary)."""
    text = html.unescape(body)
    blocks: list[dict] = []
    for match in _BLOCK_RE.finditer(text):
        block: dict = {"kind": match.group(1)}
        for line in match.group(2).splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            block[key.strip()] = value.strip()
        blocks.append(block)
    return blocks


def _rebase_f(block: dict) -> int | None:
    rounds = block.get("rounds")
    if not rounds:
        return None
    match = _REBASE_F_RE.search(rounds)
    return int(match.group(1)) if match else None


def has_explicit_escalation(body: str) -> bool:
    """An `Escalated:` line is only a signal on its own line, with nothing before the
    keyword (not a blockquote, not prefixed/negated prose) -- plan-critic note 4."""
    for line in body.splitlines():
        if line.strip().startswith(_ESCALATED_PREFIX):
            return True
    for block in extract_blocks(body):
        if block.get("kind") == "ato" and block.get("event") == "escalated":
            return True
    return False


def has_triage_answer(body: str) -> bool:
    for line in body.splitlines():
        if line.strip().startswith(_TRIAGE_HEADING):
            return True
    for block in extract_blocks(body):
        if block.get("kind") == "ato" and block.get("event") == "triage-answered":
            return True
    return False


def classify_issue(issue: IssueHistory, until: date) -> Outcome | None:
    """Classify one ticket's full history up to `until`. Returns `None` if the ticket
    never has a `started` event (nothing to classify -- contributes 0)."""
    comments = sorted(
        (c for c in issue.comments if _day(c[0]) <= until),
        key=lambda c: c[0],
    )

    started_day: date | None = None
    pending: tuple[str, date] | None = None
    resolution: Resolution | None = None
    attempt_end_day: date | None = None
    last_rebase_f = 0

    for created_at, body in comments:
        comment_day = _day(created_at)

        for block in extract_blocks(body):
            if block.get("kind") != "adev":
                continue
            event = block.get("event")
            cur_f = _rebase_f(block)
            if event == "started":
                if started_day is None:
                    started_day = comment_day
            elif event == "blocked":
                reason = (
                    "rebase-conflict-decision"
                    if cur_f is not None and cur_f > last_rebase_f
                    else "blocked"
                )
                pending = (reason, comment_day)
            elif event == "failed":
                pending = ("failed", comment_day)
            elif event == "ci-green":
                pending = None
                attempt_end_day = comment_day
            # any other adev event (review-verdict, tests-green, an unknown name, ...)
            # triggers no state transition, but still updates the rebase-f memory below
            if cur_f is not None:
                last_rebase_f = cur_f

        escalate = has_explicit_escalation(body)
        triage = has_triage_answer(body)

        if escalate:
            # An explicit escalation always wins over a triage answer in the same
            # comment (plan-critic note 1), and keeps the original pending reason.
            reason = pending[0] if pending is not None else "other"
            resolution = Resolution(day=comment_day, kind="escalated", reason=reason)
            pending = None
            attempt_end_day = comment_day
        elif triage and pending is not None:
            resolution = Resolution(day=comment_day, kind="auto_answered", reason=None)
            pending = None
            # Auto-answering a triage question does not end the attempt.

    if started_day is None:
        return None

    if pending is not None:
        # End of history: escalated on the pending event's own day. `closed_at` does
        # not clear it.
        reason, day = pending
        resolution = Resolution(day=day, kind="escalated", reason=reason)
        attempt_end_day = day

    if issue.closed_at is not None:
        closed_day = _day(issue.closed_at)
        if attempt_end_day is None or closed_day < attempt_end_day:
            attempt_end_day = closed_day

    return Outcome(start_day=started_day, resolution=resolution, attempt_end_day=attempt_end_day)


def daily_breakdown(issues: list[IssueHistory], days: list[date]) -> dict[date, dict]:
    """Per UTC day D in `days`: `in_progress` tickets (an attempt open on D, united
    with tickets escalated or auto-answered on D -- the union keeps `value` <= 1 by
    construction, per plan-critic note 3), `escalated`, `auto_answered`, `value`
    (escalated/in_progress, or `None` when in_progress is 0), and `by_reason`."""
    until = max(days)
    outcomes = [classify_issue(issue, until) for issue in issues]

    breakdown: dict[date, dict] = {}
    for day in days:
        in_progress_numbers: set[int] = set()
        escalated = 0
        auto_answered = 0
        by_reason: dict[str, int] = {}

        for issue, outcome in zip(issues, outcomes):
            if outcome is None:
                continue
            is_open = outcome.start_day <= day and (
                outcome.attempt_end_day is None or day <= outcome.attempt_end_day
            )
            resolved_today = outcome.resolution is not None and outcome.resolution.day == day
            escalated_today = resolved_today and outcome.resolution.kind == "escalated"
            auto_answered_today = resolved_today and outcome.resolution.kind == "auto_answered"

            if is_open or escalated_today or auto_answered_today:
                in_progress_numbers.add(issue.number)
            if escalated_today:
                escalated += 1
                reason = outcome.resolution.reason
                by_reason[reason] = by_reason.get(reason, 0) + 1
            if auto_answered_today:
                auto_answered += 1

        in_progress = len(in_progress_numbers)
        value = None if in_progress == 0 else round(escalated / in_progress, 4)
        breakdown[day] = {
            "in_progress": in_progress,
            "escalated": escalated,
            "auto_answered": auto_answered,
            "value": value,
            "by_reason": by_reason,
        }
    return breakdown
