"""Pure per-day pipeline-throughput and stage-cycle-time sampling (plan #14).

No GitHub I/O and no wall clock: `daily_samples` turns each ticket's ordered
`(created_at, body)` comments (an `escalations.IssueHistory`, now also carrying
`created_at`/`state_reason`/`title`) plus the repo's merged-PR map into per-UTC-day raw
counts and cycle-time samples, and `summarize` pools those samples (across days, across
repos, or both -- the caller decides) into the report shape the ticket wants: where in
the process tickets stall, not just how many move per day.

Classification (plan #14, "Approach"), reading each ticket's full history (comments are
sorted; no lower bound is applied here -- `collect._fetch_histories` already keeps a
ticket's history before `--since`, since both "first Released"/"first started" and the
pipeline/manual split need it):

  - *Bump:* the stripped, lowercased title starts with `chore(deps)`. Additive -- counted
    in every other field too, plus `dependency_bumps.created`/`closed` on the ticket's
    created/closed day.
  - *Released:* a stripped comment line starts with `## Released (gatekeeper)`. #13's
    looser `## Released` predicate (`regression_chains.py`) is untouched -- changing it
    would alter #13's own output. A `^Package:\\s*<kind>\\s+#?<id>\\s*[-\\u2013\\u2014]+\\s*<reason>`
    line on the same comment feeds `packages`, deduplicated *within one day* by
    `(kind, id)` -- a bundle's Released comment appears on every member, so counting it
    once per (kind, id) per day (not once per comment) avoids double-counting a bundle
    against `total`.
  - *Events:* `started`/`ci-green` `adev:event v1` blocks, one count per comment.
  - *Closed:* `closed_at` on day D, bucketed by `state_reason` into
    `completed`/`not_planned`/`other` (`other` also catches `None`). Independent of that,
    **every** close is `pipeline` if the ticket ever had a `ci-green` event anywhere in its
    history (before or after `closed_at`, up to `--until`), and `manual` otherwise -- the
    ticket's "closed without ever having had a ci-green" definition sets no restriction on
    the close reason, so `pipeline + manual` always equals all closes.
  - *`merged_prs`:* the count of `merged_prs` entries (`{pr_number: merged_at}`, built by
    `collect._collect_repo`) whose `merged_at` falls on D.
  - *Cycle times:* each of the five `STAGES` is "the first `<start>` to the first `<end>`
    at or after it", attributed to the *end* event's day:
      - `created_to_released`: `created_at` -> first Released comment.
      - `released_to_started`: first Released -> first `started`.
      - `started_to_pr_opened`: first `started` -> first `pr-opened`.
      - `pr_opened_to_ci_green`: first `pr-opened` -> first `ci-green`.
      - `ci_green_to_done`: first `ci-green` -> `merged_at` of the ci-green's own `pr:`
        PR if that PR is in `merged_prs`; otherwise `closed_at` if it is at or after the
        ci-green; otherwise there is no sample (an open ticket whose `pr:` PR never
        merged contributes nothing).
    A stage whose start event never happened contributes no sample for that ticket, and
    does not block any *other* stage's own (independently first-occurrence-based) sample.

Stats: `median`/`p90` pool raw per-day second counts (never per-day medians merged), then
convert to hours (`round(seconds / 3600, 4)`); `p90` is nearest-rank
(`sorted(v)[ceil(0.9n) - 1]`), matching `rounds.p90`. An empty stage is
`{n: 0, median: None, p90: None}`. `bundled_share` is `None` if `packages.total == 0`,
else `round(bundled / total, 4)`.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .escalations import IssueHistory, extract_blocks

STAGES = (
    "created_to_released",
    "released_to_started",
    "started_to_pr_opened",
    "pr_opened_to_ci_green",
    "ci_green_to_done",
)

_RELEASED_GATEKEEPER = "## Released (gatekeeper)"
_BUMP_PREFIX = "chore(deps)"
_PACKAGE_RE = re.compile(r"^Package:\s*(\S+)\s+(#?\d+)\s*[-–—]+\s*(\S+)\s*$", re.MULTILINE)


def _dt(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _is_released(body: str) -> bool:
    return any(line.strip().startswith(_RELEASED_GATEKEEPER) for line in body.splitlines())


def _package(body: str) -> tuple[str, str, str] | None:
    match = _PACKAGE_RE.search(body)
    if match is None:
        return None
    kind, pkg_id, reason = match.groups()
    return kind, pkg_id, reason


def _is_bump(title: str) -> bool:
    return title.strip().lower().startswith(_BUMP_PREFIX)


def _first_at_or_after(times: list[datetime], cutoff: datetime) -> datetime | None:
    for t in times:
        if t >= cutoff:
            return t
    return None


@dataclass(frozen=True)
class DayThroughput:
    """One repo's raw throughput counts and cycle-time samples for one UTC day --
    everything `summarize` needs to pool this day together with other days/repos.
    `packages` is already deduplicated by `(kind, id)` *within this day*; pooling
    several `DayThroughput`s concatenates their `packages` tuples rather than
    re-deduplicating across days, since a bundle re-released on a later day is a
    distinct event."""

    events: dict[str, int] = field(
        default_factory=lambda: {"released": 0, "started": 0, "ci_green": 0}
    )
    merged_prs: int = 0
    closed: dict[str, int] = field(
        default_factory=lambda: {
            "completed": 0,
            "not_planned": 0,
            "other": 0,
            "pipeline": 0,
            "manual": 0,
        }
    )
    packages: tuple[tuple[str, str, str], ...] = ()
    dependency_bumps: dict[str, int] = field(default_factory=lambda: {"created": 0, "closed": 0})
    samples: dict[str, list[float]] = field(
        default_factory=lambda: {stage: [] for stage in STAGES}
    )


def daily_samples(
    issues: list[IssueHistory],
    days: list[date],
    merged_prs: dict[int, datetime],
) -> dict[date, DayThroughput]:
    """Per UTC day D in `days`: raw event/close/package/bump counts and raw per-stage
    cycle-time samples (seconds), built from each ticket's full comment history (not
    truncated to `days` -- a ticket created or released before `days[0]` can still
    contribute a sample whose *end* event lands inside the window)."""
    days_set = set(days)
    events_by_day = {d: {"released": 0, "started": 0, "ci_green": 0} for d in days}
    merged_by_day = {d: 0 for d in days}
    closed_by_day = {
        d: {"completed": 0, "not_planned": 0, "other": 0, "pipeline": 0, "manual": 0}
        for d in days
    }
    packages_by_day: dict[date, dict[tuple[str, str], str]] = {d: {} for d in days}
    bumps_by_day = {d: {"created": 0, "closed": 0} for d in days}
    samples_by_day: dict[date, dict[str, list[float]]] = {
        d: {stage: [] for stage in STAGES} for d in days
    }

    for pr_number, merged_at in merged_prs.items():
        day = merged_at.date()
        if day in days_set:
            merged_by_day[day] += 1

    for issue in issues:
        comments = sorted(issue.comments, key=lambda c: c[0])
        is_bump = _is_bump(issue.title)

        if issue.created_at is not None:
            created_day = _dt(issue.created_at).date()
            if is_bump and created_day in days_set:
                bumps_by_day[created_day]["created"] += 1

        released_times: list[datetime] = []
        started_times: list[datetime] = []
        pr_opened_times: list[datetime] = []
        ci_green_events: list[tuple[datetime, str]] = []
        ever_ci_green = False

        for created_at, body in comments:
            comment_time = _dt(created_at)
            comment_day = comment_time.date()

            if _is_released(body):
                released_times.append(comment_time)
                if comment_day in days_set:
                    events_by_day[comment_day]["released"] += 1
                    package = _package(body)
                    if package is not None:
                        kind, pkg_id, reason = package
                        packages_by_day[comment_day][(kind, pkg_id)] = reason

            for block in extract_blocks(body):
                if block.get("kind") != "adev":
                    continue
                event = block.get("event")
                if event == "started":
                    started_times.append(comment_time)
                    if comment_day in days_set:
                        events_by_day[comment_day]["started"] += 1
                elif event == "pr-opened":
                    pr_opened_times.append(comment_time)
                elif event == "ci-green":
                    ever_ci_green = True
                    ci_green_events.append((comment_time, block.get("pr", "")))
                    if comment_day in days_set:
                        events_by_day[comment_day]["ci_green"] += 1

        if issue.closed_at is not None:
            closed_time = _dt(issue.closed_at)
            closed_day = closed_time.date()
            if closed_day in days_set:
                reason_bucket = closed_by_day[closed_day]
                if issue.state_reason == "completed":
                    reason_bucket["completed"] += 1
                elif issue.state_reason == "not_planned":
                    reason_bucket["not_planned"] += 1
                else:
                    reason_bucket["other"] += 1
                if ever_ci_green:
                    reason_bucket["pipeline"] += 1
                else:
                    reason_bucket["manual"] += 1
                if is_bump:
                    bumps_by_day[closed_day]["closed"] += 1

        # -- cycle times: each stage independently first-occurrence-based -------
        if issue.created_at is not None and released_times:
            created_time = _dt(issue.created_at)
            end = _first_at_or_after(released_times, created_time)
            if end is not None and end.date() in days_set:
                samples_by_day[end.date()]["created_to_released"].append(
                    (end - created_time).total_seconds()
                )

        if released_times and started_times:
            start = released_times[0]
            end = _first_at_or_after(started_times, start)
            if end is not None and end.date() in days_set:
                samples_by_day[end.date()]["released_to_started"].append(
                    (end - start).total_seconds()
                )

        if started_times and pr_opened_times:
            start = started_times[0]
            end = _first_at_or_after(pr_opened_times, start)
            if end is not None and end.date() in days_set:
                samples_by_day[end.date()]["started_to_pr_opened"].append(
                    (end - start).total_seconds()
                )

        if pr_opened_times and ci_green_events:
            start = pr_opened_times[0]
            end = _first_at_or_after([t for t, _pr in ci_green_events], start)
            if end is not None and end.date() in days_set:
                samples_by_day[end.date()]["pr_opened_to_ci_green"].append(
                    (end - start).total_seconds()
                )

        if ci_green_events:
            start, pr_raw = ci_green_events[0]
            done_time: datetime | None = None
            pr_number: int | None = None
            if pr_raw:
                try:
                    pr_number = int(pr_raw)
                except ValueError:
                    pr_number = None
            if pr_number is not None and pr_number in merged_prs:
                done_time = merged_prs[pr_number]
            elif issue.closed_at is not None:
                closed_time = _dt(issue.closed_at)
                if closed_time >= start:
                    done_time = closed_time
            if done_time is not None and done_time.date() in days_set:
                samples_by_day[done_time.date()]["ci_green_to_done"].append(
                    (done_time - start).total_seconds()
                )

    return {
        day: DayThroughput(
            events=events_by_day[day],
            merged_prs=merged_by_day[day],
            closed=closed_by_day[day],
            packages=tuple(
                (kind, pkg_id, reason)
                for (kind, pkg_id), reason in packages_by_day[day].items()
            ),
            dependency_bumps=bumps_by_day[day],
            samples=samples_by_day[day],
        )
        for day in days
    }


def _stage_stat(seconds: list[float]) -> dict:
    if not seconds:
        return {"n": 0, "median": None, "p90": None}
    ordered = sorted(seconds)
    p90_index = math.ceil(0.9 * len(ordered)) - 1
    return {
        "n": len(seconds),
        "median": round(statistics.median(seconds) / 3600, 4),
        "p90": round(ordered[p90_index] / 3600, 4),
    }


def summarize(day_samples: list[DayThroughput]) -> dict:
    """Pool a list of `DayThroughput` (across days, across repos, or both -- the caller
    decides) into the output report shape. Pooling happens on the raw counts/samples,
    never on already-computed per-group stats (median/p90 cannot be merged from
    summaries, same rationale as `rounds.summarize`)."""
    events = {"released": 0, "started": 0, "ci_green": 0}
    merged_prs_total = 0
    closed = {"completed": 0, "not_planned": 0, "other": 0, "pipeline": 0, "manual": 0}
    packages: list[tuple[str, str, str]] = []
    bumps = {"created": 0, "closed": 0}
    samples: dict[str, list[float]] = {stage: [] for stage in STAGES}

    for day in day_samples:
        for key in events:
            events[key] += day.events[key]
        merged_prs_total += day.merged_prs
        for key in closed:
            closed[key] += day.closed[key]
        packages.extend(day.packages)
        for key in bumps:
            bumps[key] += day.dependency_bumps[key]
        for stage in STAGES:
            samples[stage].extend(day.samples[stage])

    total = len(packages)
    bundled = sum(1 for kind, _pkg_id, _reason in packages if kind != "single")
    by_reason: dict[str, int] = {}
    for _kind, _pkg_id, reason in packages:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    bundled_share = None if total == 0 else round(bundled / total, 4)

    return {
        "events": events,
        "merged_prs": merged_prs_total,
        "closed": closed,
        "stages": {stage: _stage_stat(samples[stage]) for stage in STAGES},
        "packages": {
            "total": total,
            "bundled": bundled,
            "bundled_share": bundled_share,
            "by_reason": by_reason,
        },
        "dependency_bumps": bumps,
    }
