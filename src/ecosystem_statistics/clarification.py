"""Pure per-day "clarification" breakdown for the gatekeeper's release comments (plan #17,
children #15/#8).

No GitHub I/O and no wall clock: `daily_breakdown` turns a repo's `escalations.IssueHistory`
list into a per-UTC-day breakdown of how often the gatekeeper released a package without
asking a human, and how heavy the clarifications, frames, lane splits, re-cuts and
falsified premises were. Reuses `escalations.IssueHistory` and `escalations._day` -- there
is no `metrics/` package, siblings (`regression_chains.py`, `throughput.py`) are flat
modules next to `escalations.py`.

Classification is by comment content only, never by author (AGENTS.md: the pipeline's own
GitHub author is always the human account that ran it). A stripped comment line must match
`^##\\s+(Released|Clarification needed|Frame|Lane split|Re-cut)\\s+\\(gatekeeper\\)` -- the
`(gatekeeper)` suffix is required, so `## Released in v1.2` and `## Framework (gatekeeper)`
(a `Frame` prefix collision) both add 0. Each heading kind counts at most once per comment.

Marker lines (`Not proven by this package:`, `PREMISE FALSIFIED:`) match
`^[\\s>*+_-]*<marker>` against each raw line: bulleted, bold and blockquoted markers count;
mid-sentence and backticked mentions do not, since the leading-whitespace/bullet character
class cannot reach past a non-matching prefix character. `premise_falsified` is scanned in
*any* comment, regardless of heading; `not_proven` is only scanned inside a `Frame`
comment (whether or not a `<!-- gatekeeper:frame v1 -->` block is present) -- the block's
own `not_proven` field is never read, only `ac_rewritten`/`premises` are, and a non-digit
`premises` value counts as 0 (a malformed block must not crash the nightly run).

"Released without asking" is tracked per *package*, not per ticket, since the repo has no
package model -- the only record of grouping is the Released comment's own `Package:`
line (`Package: <kind> #<id> (children #a, #b)`, the `(children ...)` suffix optional).
Every comment across every ticket is walked once, in `(created_at, ticket number, comment
index)` order, with a running `asked: dict[int, bool]` state set by `Clarification needed`
comments and cleared by every `Released` comment for all of its members (`T`, the package
id, and each child) -- so a Clarification posted on a child ticket counts as asking for the
whole package. A `(key, day)` pair is counted at most once (`key` = the package id, or `T`
when the comment has no `Package:` line at all) -- a bundle's Released comment can appear
on every member, and without this dedup a bundle would inflate `released`. When one comment
carries both headings, the Clarification is processed first, so a combined
clarify-and-release comment reads as "asked".

History from before `--since` (kept by `collect._fetch_histories` to feed state) updates
`asked` but is never counted, since its own day is not in `days`.
"""

from __future__ import annotations

import html
import re
from datetime import date

from .escalations import IssueHistory, _day

_HEADING_RE = re.compile(
    r"^##\s+(Released|Clarification needed|Frame|Lane split|Re-cut)\s+\(gatekeeper\)"
)
_QUESTION_RE = re.compile(r"^###\s+Q\d+\b")
# The mandatory literal newline right after `v1` (rather than a bare `\b`) is what tells
# the real, multi-line machine block apart from a backticked, single-line, mid-sentence
# mention of the same opening marker (e.g. "the `<!-- gatekeeper:frame v1 -->` block"
# inside a Frame's own "Premises to verify" prose) -- the latter has no newline before its
# `-->`, so it never matches, and `re.search`'s leftmost-match never picks it over the
# real block later in the same comment.
_FRAME_BLOCK_RE = re.compile(r"<!--\s*gatekeeper:frame\s+v1[ \t]*\n(.*?)-->", re.DOTALL)
_PACKAGE_LINE_RE = re.compile(
    r"^Package:\s*\S+\s+#?(\d+)(?:\s*\(children\s+([^)]*)\))?", re.MULTILINE
)

_NOT_PROVEN_MARKER = "Not proven by this package:"
_PREMISE_FALSIFIED_MARKER = "PREMISE FALSIFIED:"

_HARD_THRESHOLD = 4  # the single place the "4+ questions" rule lives

_ZERO_COUNTS = {
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
}


def _headings(body: str) -> set[str]:
    found: set[str] = set()
    for line in body.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            found.add(match.group(1))
    return found


def _count_questions(body: str) -> int:
    return sum(1 for line in body.splitlines() if _QUESTION_RE.match(line.strip()))


def _marker_lines(body: str, marker: str) -> int:
    pattern = re.compile(r"^[\s>*+_-]*" + re.escape(marker))
    return sum(1 for line in body.splitlines() if pattern.match(line))


def _safe_int(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _frame_block(body: str) -> dict[str, str] | None:
    text = html.unescape(body)
    match = _FRAME_BLOCK_RE.search(text)
    if match is None:
        return None
    block: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        block[key.strip()] = value.strip()
    return block


def _package_key_and_members(ticket_number: int, body: str) -> tuple[int, set[int]]:
    """The Released comment's dedup key and the set of tickets it covers -- `T`, the
    package id, and each `#n` among the children (F2). No `Package:` line at all falls
    back to the ticket's own number for both."""
    match = _PACKAGE_LINE_RE.search(body)
    if match is None:
        return ticket_number, {ticket_number}
    package_id = int(match.group(1))
    members = {ticket_number, package_id}
    children_raw = match.group(2)
    if children_raw:
        members.update(int(n) for n in re.findall(r"\d+", children_raw))
    return package_id, members


def referenced_package_members(issues: list[IssueHistory]) -> set[int]:
    """Every ticket number named as a package member (`T`, the package id, or a
    child) by a Released (gatekeeper) comment anywhere in `issues`. Used by
    `collect._fetch_clarification_histories` to find which members
    `collect._fetch_histories`' closed-before-`--since` skip may have dropped
    entirely (not merely filtered by comment date, but never fetched at all), so a
    real Clarification comment on an early-closed child ticket doesn't silently go
    missing from the "asked" state (reviewer finding R1, package #17)."""
    referenced: set[int] = set()
    for issue in issues:
        for _created_at, body in issue.comments:
            if "Released" not in _headings(body):
                continue
            _key, members = _package_key_and_members(issue.number, body)
            referenced.update(members)
    return referenced


def daily_breakdown(issues: list[IssueHistory], days: list[date]) -> dict[date, dict]:
    """Per UTC day D in `days`: how often the gatekeeper released a package without
    asking a human, and how heavy the clarifications/frames/lane-splits/re-cuts/
    falsified-premises were. See the module docstring for the full rules."""
    days_set = set(days)
    counts_by_day: dict[date, dict] = {day: dict(_ZERO_COUNTS) for day in days}

    ordered_comments: list[tuple[str, int, int, str]] = []
    for issue in issues:
        for idx, (created_at, body) in enumerate(issue.comments):
            ordered_comments.append((created_at, issue.number, idx, body))
    ordered_comments.sort(key=lambda c: (c[0], c[1], c[2]))

    asked: dict[int, bool] = {}
    seen_release_keys: set[tuple[int, date]] = set()

    for created_at, ticket_number, _idx, body in ordered_comments:
        day = _day(created_at)
        counted_day = day in days_set
        headings = _headings(body)

        if "Clarification needed" in headings:
            asked[ticket_number] = True
            if counted_day:
                n_questions = _count_questions(body)
                counts = counts_by_day[day]
                counts["clarifications"] += 1
                counts["questions"] += n_questions
                if n_questions >= _HARD_THRESHOLD:
                    counts["hard_clarifications"] += 1

        if "Frame" in headings:
            not_proven = _marker_lines(body, _NOT_PROVEN_MARKER)
            block = _frame_block(body)
            if counted_day:
                counts = counts_by_day[day]
                counts["frames"] += 1
                counts["not_proven"] += not_proven
                if block is not None:
                    if block.get("ac_rewritten") == "yes":
                        counts["frames_ac_rewritten"] += 1
                    counts["premises"] += _safe_int(block.get("premises"))
                else:
                    counts["frames_without_block"] += 1

        if "Lane split" in headings and counted_day:
            counts_by_day[day]["lane_splits"] += 1

        if "Re-cut" in headings and counted_day:
            counts_by_day[day]["re_cuts"] += 1

        premise_falsified = _marker_lines(body, _PREMISE_FALSIFIED_MARKER)
        if premise_falsified and counted_day:
            counts_by_day[day]["premise_falsified"] += premise_falsified

        if "Released" in headings:
            key, members = _package_key_and_members(ticket_number, body)
            dedup_key = (key, day)
            if counted_day and dedup_key not in seen_release_keys:
                seen_release_keys.add(dedup_key)
                counts = counts_by_day[day]
                counts["released"] += 1
                if not any(asked.get(member, False) for member in members):
                    counts["released_without_asking"] += 1
            for member in members:
                asked[member] = False

    for day in days:
        counts = counts_by_day[day]
        released = counts["released"]
        counts["value"] = (
            None if released == 0 else round(counts["released_without_asking"] / released, 4)
        )

    return counts_by_day
