"""Pure regression-chain parsing and daily rollup (plan #13).

No GitHub I/O and no wall clock: `chain_members` turns one comment body into the set of
tickets a regression chain names (or `None` when the comment is not a chain comment at
all), and `daily_breakdown` turns a repo's `escalations.IssueHistory` list into a
per-UTC-day `active`/`chain_active`/`value`/`detected`/`chains` breakdown.

A comment is a *chain comment* only when a stripped line starts with the gatekeeper's
`## Regression chain (gatekeeper)` heading (plan #13, "Approach"). Once gated, its
members are the **union** of two sources, read from the comment body after
`html.unescape` (the same escaped/fenced/raw tolerance as `escalations.extract_blocks`):
  - the first-cell ref of every Markdown table row (header/separator rows never match,
    since neither cell is a bare or qualified `#N`);
  - the comma-separated `members:` line of every `<!-- gatekeeper:chain v1 ... -->`
    block.
A bare `#N` ref is qualified against the comment's own repo (`<repo>#N`); a ref that
already carries an `owner/repo` prefix is kept as-is. The chain's length is the size of
this de-duplicated union, never the sum of the two sources.

`daily_breakdown`'s `active` predicate (plan #13, "Approach") counts a ticket as active
on day D when at least one of its comments on D is a chain comment, has a non-empty
`escalations.extract_blocks` result (an adev/ato machine event), or has a stripped line
starting with `## Released` -- or when the ticket's `closed_at` falls on D. A plain
comment with none of these never counts. `chain_active` is active tickets that also
carry the `regression-chain` label (current state, per `IssueHistory.labels`);
`detected`/`chains` count/list the chain comments posted on D, independent of whether
the posting ticket is itself active that day.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone

from .escalations import IssueHistory, extract_blocks

_CHAIN_HEADING = "## Regression chain (gatekeeper)"
_RELEASED_HEADING = "## Released"

_ROW_RE = re.compile(r"^\s*\|\s*((?:[\w.-]+/[\w.-]+)?#\d+)\s*\|", re.MULTILINE)
_CHAIN_BLOCK_RE = re.compile(r"<!--\s*gatekeeper:chain\s+v1\b(.*?)-->", re.DOTALL)


def _day(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).date()


def _normalize(ref: str, repo: str) -> str:
    return ref if "/" in ref else f"{repo}{ref}"


def _block_member_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in _CHAIN_BLOCK_RE.finditer(text):
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key.strip() != "members":
                continue
            refs.extend(part.strip() for part in value.split(",") if part.strip())
    return refs


def chain_members(body: str, repo: str) -> frozenset[str] | None:
    """`None` unless a stripped line in `body` starts with the gatekeeper's chain
    heading; otherwise the de-duplicated union of table-row refs and block `members:`
    refs, each qualified against `repo`."""
    if not any(line.strip().startswith(_CHAIN_HEADING) for line in body.splitlines()):
        return None

    text = html.unescape(body)
    members = {_normalize(ref, repo) for ref in _ROW_RE.findall(text)}
    members.update(_normalize(ref, repo) for ref in _block_member_refs(text))
    return frozenset(members)


def daily_breakdown(issues: list[IssueHistory], days: list[date], repo: str) -> dict[date, dict]:
    """Per UTC day D in `days`: `active`/`chain_active`/`value` (share of active tickets
    carrying the `regression-chain` label, or `None` when `active` is 0), `detected`
    (chain comments posted on D) and `chains` (`{"ticket": "<repo>#N", "length": k}`,
    sorted by ticket)."""
    days_set = set(days)
    active_by_day: dict[date, set[int]] = {day: set() for day in days}
    detected_by_day: dict[date, int] = {day: 0 for day in days}
    chains_by_day: dict[date, list[dict]] = {day: [] for day in days}

    for issue in issues:
        for created_at, body in issue.comments:
            comment_day = _day(created_at)
            if comment_day not in days_set:
                continue

            members = chain_members(body, repo)
            is_chain = members is not None
            is_event = bool(extract_blocks(body))
            is_released = any(
                line.strip().startswith(_RELEASED_HEADING) for line in body.splitlines()
            )

            if is_chain or is_event or is_released:
                active_by_day[comment_day].add(issue.number)
            if is_chain:
                detected_by_day[comment_day] += 1
                chains_by_day[comment_day].append(
                    {"ticket": f"{repo}#{issue.number}", "length": len(members)}
                )

        if issue.closed_at is not None:
            closed_day = _day(issue.closed_at)
            if closed_day in days_set:
                active_by_day[closed_day].add(issue.number)

    breakdown: dict[date, dict] = {}
    for day in days:
        active_numbers = active_by_day[day]
        active = len(active_numbers)
        chain_active = sum(
            1
            for issue in issues
            if issue.number in active_numbers and "regression-chain" in issue.labels
        )
        value = None if active == 0 else round(chain_active / active, 4)
        breakdown[day] = {
            "active": active,
            "chain_active": chain_active,
            "value": value,
            "detected": detected_by_day[day],
            "chains": sorted(chains_by_day[day], key=lambda c: c["ticket"]),
        }
    return breakdown
