"""Pure per-ticket critic/review/CI round-count sampling and lane statistics (plan #12).

No GitHub I/O and no wall clock: `daily_samples` turns each ticket's ordered
`(created_at, body)` comments (an `escalations.IssueHistory`) into per-UTC-day,
per-lane round-count samples, and `summarize` pools those samples (across days or
across repos -- the caller decides which) into the avg/median/p90 report shape the
ticket wants.

Session/lane rules (plan #12, "Approach"), applied per ticket, walking its comments in
time order:
  - On `started`: reset `last_rounds` to `None` and increment the ticket's running
    `started` count. A `started` block always carries its own (irrelevant, always-zero)
    `rounds:` line -- see the tie-break note on `daily_samples` below for why that line
    is never used as a snapshot.
  - On any other `adev` block with a `rounds:` line: set `last_rounds` to that line.
  - On a terminal event (`ci-green`/`blocked`/`failed`) on day D: this is a "session"
    ending on D. If `last_rounds` is set, parse it and classify it into a lane by its
    gate names (`scenario-critic` present -> prose, `plan-critic` present -> dev,
    otherwise the sample is skipped -- an unknown snapshot is never fabricated into
    either lane). If `last_rounds` is `None` (no snapshot taken yet this session), the
    lane sample is skipped too, but the *ticket-level* session count below still
    includes this terminal.
  - Ticket-level `sessions` (F2): for every ticket with at least one terminal event on
    day D, n = the number of `started` events (over the ticket's *full* history, not
    just day D) up to its *last* terminal event on D. `sessions = {tickets: k, started:
    sum(n), per_ticket: sum(n)/k}` -- computed after pooling across days/repos, never
    as an average of per-ticket ratios.

Counting schemes (F1, never mixed): dev's `used` is the gate's literal `used` field
(so it includes clean `(0f,0i)` rounds); prose's `used` is redefined as `f + i`, the
ticket's own prose definition and the prose cap basis, which is the same in both the
old and new prompt-engineer emitter eras (only the *literal* `used` field changed
across that boundary). Each lane's `counting` field in the output says which scheme it
used ("used" or "f+i").

Stats: `avg` and `median` are `round(x, 4)`; `p90` is nearest-rank
(`sorted(v)[ceil(0.9n) - 1]`), a raw int. `over_soft_cap_share` and `infra_share` pool
every (gate, session) entry in the lane together (not per gate): `over_soft_cap_share`
= entries with counted-`used` > `soft` / entries with counted-`used` >= 1;
`infra_share` = sum(i) / sum(counted-`used`) across the same pooled entries. A ratio
with a zero denominator is `None`. Both lanes are always present in `summarize`'s
output; an empty lane has `gates: {}`.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .escalations import Gate, IssueHistory, extract_blocks, parse_rounds

TERMINAL_EVENTS = frozenset({"ci-green", "blocked", "failed"})


def _day(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).date()


def _lane_for(gates: dict[str, Gate]) -> str | None:
    if "scenario-critic" in gates:
        return "prose"
    if "plan-critic" in gates:
        return "dev"
    return None


@dataclass(frozen=True)
class DaySamples:
    """One repo's raw round-count samples for one UTC day -- everything `summarize`
    needs to pool this day together with other days/repos. `lane_samples["dev"|"prose"]`
    is one `dict[gate_name, Gate]` per sampled session (never pre-aggregated), because
    median and p90 cannot be recomputed from an already-summarized block."""

    replan_triggered: int
    session_tickets: int
    session_started: int
    lane_samples: dict[str, list[dict[str, Gate]]] = field(
        default_factory=lambda: {"dev": [], "prose": []}
    )


def daily_samples(issues: list[IssueHistory], days: list[date]) -> dict[date, DaySamples]:
    """Per UTC day D in `days`: the raw `replan-triggered` count, the ticket-level
    session totals (F2), and the raw per-lane samples (one `dict[gate, Gate]` per
    sampled terminal event)."""
    days_set = set(days)
    replan_by_day: dict[date, int] = {day: 0 for day in days}
    tickets_by_day: dict[date, int] = {day: 0 for day in days}
    started_by_day: dict[date, int] = {day: 0 for day in days}
    lane_samples_by_day: dict[date, dict[str, list[dict[str, Gate]]]] = {
        day: {"dev": [], "prose": []} for day in days
    }

    for issue in issues:
        comments = sorted(issue.comments, key=lambda c: c[0])
        running_started = 0
        last_rounds: str | None = None
        # The last terminal event's `n` (running `started` count) seen so far, per
        # day -- overwritten as we walk forward, so it ends up holding *the ticket's
        # last terminal event on that day*, per F2.
        last_n_by_day: dict[date, int] = {}

        for created_at, body in comments:
            comment_day = _day(created_at)

            for block in extract_blocks(body):
                if block.get("kind") != "adev":
                    continue
                event = block.get("event")

                if event == "started":
                    running_started += 1
                    # Tie-break (test-critic round 1, note 1): a `started` block's own
                    # `rounds:` line is never treated as a snapshot -- the reset always
                    # wins over the generic "any block with a rounds: line" rule for
                    # this same block, since both rules would otherwise fire on it.
                    last_rounds = None
                    continue

                rounds_line = block.get("rounds")
                if rounds_line:
                    last_rounds = rounds_line

                if event == "replan-triggered":
                    if comment_day in days_set:
                        replan_by_day[comment_day] += 1
                elif event in TERMINAL_EVENTS:
                    if comment_day in days_set:
                        last_n_by_day[comment_day] = running_started
                        if last_rounds:
                            gates = parse_rounds(last_rounds)
                            lane = _lane_for(gates)
                            if lane is not None:
                                lane_samples_by_day[comment_day][lane].append(gates)

        for day, n in last_n_by_day.items():
            tickets_by_day[day] += 1
            started_by_day[day] += n

    return {
        day: DaySamples(
            replan_triggered=replan_by_day[day],
            session_tickets=tickets_by_day[day],
            session_started=started_by_day[day],
            lane_samples=lane_samples_by_day[day],
        )
        for day in days
    }


def p90(values: list[int]) -> int:
    """Nearest-rank p90: `sorted(v)[ceil(0.9n) - 1]`."""
    ordered = sorted(values)
    index = math.ceil(0.9 * len(ordered)) - 1
    return ordered[index]


def median(values: list[int]) -> float:
    return statistics.median(values)


def _stat_block(values: list[int]) -> dict:
    return {
        "avg": round(statistics.mean(values), 4),
        "median": round(median(values), 4),
        "p90": p90(values),
    }


def _counted_used(gate: Gate, counting: str) -> int:
    return gate.used if counting == "used" else gate.f + gate.i


def _summarize_lane(samples: list[dict[str, Gate]], counting: str) -> dict:
    gate_names: set[str] = set()
    for sample in samples:
        gate_names.update(sample.keys())

    gates_out: dict[str, dict] = {}
    for name in sorted(gate_names):
        used_values = [_counted_used(sample[name], counting) for sample in samples if name in sample]
        f_values = [sample[name].f for sample in samples if name in sample]
        i_values = [sample[name].i for sample in samples if name in sample]
        gates_out[name] = {
            "used": _stat_block(used_values),
            "f": _stat_block(f_values),
            "i": _stat_block(i_values),
        }

    # over_soft_cap_share and infra_share pool every (gate, session) entry in the lane
    # together, not per gate (plan #12, "Stats").
    entries = [
        (_counted_used(gate, counting), gate.soft, gate.i)
        for sample in samples
        for gate in sample.values()
    ]
    over_count = sum(1 for used, _soft, _i in entries if used >= 1)
    over_soft_count = sum(1 for used, soft, _i in entries if used > soft)
    over_soft_cap_share = None if over_count == 0 else round(over_soft_count / over_count, 4)

    sum_used = sum(used for used, _soft, _i in entries)
    sum_i = sum(i for _used, _soft, i in entries)
    infra_share = None if sum_used == 0 else round(sum_i / sum_used, 4)

    return {
        "counting": counting,
        "sessions": len(samples),
        "over_soft_cap_share": over_soft_cap_share,
        "infra_share": infra_share,
        "gates": gates_out,
    }


def summarize(day_samples: list[DaySamples]) -> dict:
    """Pool a list of `DaySamples` (across days, across repos, or both -- the caller
    decides) into the output report shape. Pooling happens on the raw samples, never
    on already-computed per-group averages (R3): median and p90 cannot be merged from
    summaries."""
    replan_triggered = sum(d.replan_triggered for d in day_samples)
    tickets = sum(d.session_tickets for d in day_samples)
    started = sum(d.session_started for d in day_samples)
    per_ticket = None if tickets == 0 else round(started / tickets, 4)

    lanes = {}
    for lane, counting in (("dev", "used"), ("prose", "f+i")):
        pooled = [sample for d in day_samples for sample in d.lane_samples.get(lane, [])]
        lanes[lane] = _summarize_lane(pooled, counting)

    return {
        "replan_triggered": replan_triggered,
        "sessions": {"tickets": tickets, "started": started, "per_ticket": per_ticket},
        "lanes": lanes,
    }
