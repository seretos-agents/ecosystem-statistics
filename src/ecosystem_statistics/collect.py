"""Orchestrates one `collect` run: clone/fetch each configured repo, compute branch_churn
(per merged PR) and main_rework (per UTC day) for every day in `[since, until]`, and write
deterministic daily JSON files plus a cumulative index (plan #9)."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import (
    churn,
    clarification,
    config,
    escalations,
    github,
    gitrepo,
    output,
    regression_chains,
    rounds,
    throughput,
)


def _daterange(since: date, until: date) -> list[date]:
    days = []
    day = since
    while day <= until:
        days.append(day)
        day += timedelta(days=1)
    return days


def _round4(value: float) -> float:
    return round(value, 4)


def _parse_day(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).date()


def _merge_branch_churn(blocks: list[dict]) -> dict:
    gross = sum(b["gross_added"] for b in blocks)
    net = sum(b["net_added"] for b in blocks)
    prs = sum(b["prs"] for b in blocks)
    value = None if gross == 0 else _round4(1 - net / gross)
    return {"gross_added": gross, "net_added": net, "prs": prs, "value": value}


def _merge_main_rework(blocks: list[dict], window_days: int) -> dict:
    young = sum(b["young_removed"] for b in blocks)
    added = sum(b["added_to_main"] for b in blocks)
    value = None if added == 0 else _round4(young / added)
    return {
        "young_removed": young,
        "added_to_main": added,
        "window_days": window_days,
        "value": value,
    }


def _merge_escalations(blocks: list[dict]) -> dict:
    """Sum `in_progress`/`escalated`/`auto_answered` and `by_reason` across repos, then
    recompute `value` from the summed counts -- never last-write-wins (test-critic
    note: a single-repo fixture cannot itself distinguish a real sum from a copy)."""
    in_progress = sum(b["in_progress"] for b in blocks)
    escalated = sum(b["escalated"] for b in blocks)
    auto_answered = sum(b["auto_answered"] for b in blocks)
    by_reason: dict[str, int] = {}
    for block in blocks:
        for reason, count in block["by_reason"].items():
            by_reason[reason] = by_reason.get(reason, 0) + count
    value = None if in_progress == 0 else _round4(escalated / in_progress)
    return {
        "in_progress": in_progress,
        "escalated": escalated,
        "auto_answered": auto_answered,
        "value": value,
        "by_reason": by_reason,
    }


_CLARIFICATION_INT_KEYS = (
    "released",
    "released_without_asking",
    "clarifications",
    "questions",
    "hard_clarifications",
    "frames",
    "frames_ac_rewritten",
    "premises",
    "not_proven",
    "frames_without_block",
    "lane_splits",
    "re_cuts",
    "premise_falsified",
)


def _merge_clarification(blocks: list[dict]) -> dict:
    """Sum every integer counter across repos and recompute `value` from the summed
    counts -- never last-write-wins (same rationale as `_merge_escalations`)."""
    merged = {key: sum(b[key] for b in blocks) for key in _CLARIFICATION_INT_KEYS}
    released = merged["released"]
    merged["value"] = (
        None if released == 0 else _round4(merged["released_without_asking"] / released)
    )
    return merged


def _merge_regression_chains(blocks: list[dict]) -> dict:
    """Sum `active`/`chain_active`/`detected` and concatenate+sort `chains` across
    repos, then recompute `value` from the summed counts -- never last-write-wins (same
    rationale as `_merge_escalations`)."""
    active = sum(b["active"] for b in blocks)
    chain_active = sum(b["chain_active"] for b in blocks)
    detected = sum(b["detected"] for b in blocks)
    chains = sorted((c for b in blocks for c in b["chains"]), key=lambda c: c["ticket"])
    value = None if active == 0 else _round4(chain_active / active)
    return {
        "active": active,
        "chain_active": chain_active,
        "value": value,
        "detected": detected,
        "chains": chains,
    }


def _fetch_histories(
    repo_cfg: config.RepoConfig,
    *,
    days: list[date],
    client,
) -> list[escalations.IssueHistory]:
    """Every issue's classification-relevant history for one repo -- shared by both
    #11's `escalations.daily_breakdown` and #12's `rounds.daily_samples`. Fetches
    comments only for issues created on or before the end of `--until` and not closed
    before `--since` (otherwise every historical issue's comments are fetched every
    night), and drops any fetched comment posted after the end of `--until` (otherwise
    a rerun of the same window could see newer comments and give different bytes) --
    history before `--since` is kept, since both #11's verdicts and #12's sessions use
    each ticket's full history up to `--until`.

    This closed-before-`--since` skip drops a matching issue's comments entirely, not
    just the ones outside the window -- fine for #11-#14, which only ever look at
    in-window activity plus each ticket's own history. #17's clarification metric is
    the exception: a package's "asked" state can be set by a Clarification comment on
    a *child* ticket that closed (and was skipped here) long before `--since`.
    `_fetch_clarification_histories` tops this list up for that one metric, rather
    than widening the skip below and reintroducing the "fetch every historical
    issue's comments every night" cost for all four other metrics."""
    since, until = days[0], days[-1]
    issues_meta = github.list_issues(client, repo_cfg.owner, repo_cfg.name)

    histories: list[escalations.IssueHistory] = []
    for meta in issues_meta:
        if _parse_day(meta.created_at) > until:
            continue
        if meta.closed_at is not None and _parse_day(meta.closed_at) < since:
            continue
        raw_comments = github.list_issue_comments(
            client, repo_cfg.owner, repo_cfg.name, meta.number
        )
        comments = tuple(
            (comment_created_at, body)
            for comment_created_at, body in raw_comments
            if _parse_day(comment_created_at) <= until
        )
        histories.append(
            escalations.IssueHistory(
                number=meta.number,
                closed_at=meta.closed_at,
                comments=comments,
                labels=meta.labels,
                created_at=meta.created_at,
                state_reason=meta.state_reason,
                title=meta.title,
            )
        )

    return histories


def _fetch_clarification_histories(
    repo_cfg: config.RepoConfig,
    *,
    days: list[date],
    client,
    base_histories: list[escalations.IssueHistory],
) -> list[escalations.IssueHistory]:
    """`base_histories` (the same filtered set #11-#14 use) plus a targeted top-up for
    any package member `_fetch_histories`' closed-before-`--since` skip dropped
    entirely (reviewer finding R1, package #17): a Clarification comment on a child
    ticket closed long before `--since` must still reach the "asked" tracking below,
    or an epic's later in-window Released comment naming that child would wrongly
    count the package as released without asking.

    Fetches comments only for the specific tickets `clarification.
    referenced_package_members` finds named as a package member by a Released
    comment already present in `base_histories`, and not themselves present -- not
    every historical closed issue in the repo -- so the other four metrics'
    `_fetch_histories` call, and its performance characteristics, are untouched, and
    the common case (no such gap) makes no extra request at all."""
    present = {issue.number for issue in base_histories}
    missing = clarification.referenced_package_members(base_histories) - present
    if not missing:
        return base_histories

    until = days[-1]
    issues_meta_by_number = {
        meta.number: meta
        for meta in github.list_issues(client, repo_cfg.owner, repo_cfg.name)
    }
    extra: list[escalations.IssueHistory] = []
    for number in sorted(missing):
        meta = issues_meta_by_number.get(number)
        if meta is None:
            continue
        raw_comments = github.list_issue_comments(
            client, repo_cfg.owner, repo_cfg.name, meta.number
        )
        comments = tuple(
            (comment_created_at, body)
            for comment_created_at, body in raw_comments
            if _parse_day(comment_created_at) <= until
        )
        extra.append(
            escalations.IssueHistory(
                number=meta.number,
                closed_at=meta.closed_at,
                comments=comments,
                labels=meta.labels,
                created_at=meta.created_at,
                state_reason=meta.state_reason,
                title=meta.title,
            )
        )
    return list(base_histories) + extra


def _collect_repo(
    repo_cfg: config.RepoConfig,
    *,
    days: list[date],
    cache_dir: Path,
    churn_cfg: config.ChurnConfig,
    client,
    token: str | None,
) -> tuple[dict[date, dict], dict[date, rounds.DaySamples], dict[date, throughput.DayThroughput]]:
    """branch_churn + main_rework + escalations + rounds + throughput blocks for one
    repo, for every day in `days`. Also returns the day's raw `rounds.DaySamples` and
    `throughput.DayThroughput`, so `run_collect` can pool them across repos for
    `totals["rounds"]`/`totals["throughput"]` -- median/p90 cannot be recomputed from an
    already-summarized per-repo block."""
    repo_path = gitrepo.ensure_repo(
        repo_cfg.clone_url, cache_dir, repo_cfg.owner, repo_cfg.name, token=token
    )
    full_name = f"{repo_cfg.owner}/{repo_cfg.name}"
    default_branch = gitrepo.default_branch(repo_path)

    prs = github.list_merged_prs(
        client, repo_cfg.owner, repo_cfg.name, default_branch, days[0], days[-1]
    )
    merged_prs_by_number = {pr.number: pr.merged_at for pr in prs}
    histories = _fetch_histories(repo_cfg, days=days, client=client)
    escalations_by_day = escalations.daily_breakdown(histories, days)
    rounds_by_day = rounds.daily_samples(histories, days)
    regression_chains_by_day = regression_chains.daily_breakdown(histories, days, full_name)
    throughput_by_day = throughput.daily_samples(histories, days, merged_prs_by_number)
    clarification_histories = _fetch_clarification_histories(
        repo_cfg, days=days, client=client, base_histories=histories
    )
    clarification_by_day = clarification.daily_breakdown(clarification_histories, days)
    per_pr_by_day: dict[date, list[tuple[int, str, churn.BranchChurnResult]]] = {
        day: [] for day in days
    }
    for pr in prs:
        parents = gitrepo.rev_parents(repo_path, pr.merge_commit_sha)
        if len(parents) != 2:
            print(
                f"skip PR #{pr.number} in {full_name}: {pr.merge_commit_sha} is not a "
                "two-parent merge commit",
                file=sys.stderr,
            )
            continue
        result = churn.branch_churn(
            repo_path, pr.merge_commit_sha, exclude_paths=churn_cfg.exclude_paths
        )
        per_pr_by_day[pr.merged_at.date()].append((pr.number, pr.merge_commit_sha, result))

    per_day: dict[date, dict] = {}
    for day in days:
        entries = sorted(per_pr_by_day[day], key=lambda e: e[0])
        branch_block = _merge_branch_churn(
            [
                {
                    "gross_added": result.gross_added,
                    "net_added": result.net_added,
                    "prs": 1,
                    "value": result.value,
                }
                for _, _, result in entries
            ]
        )
        branch_block["per_pr"] = [
            {
                "number": number,
                "merge_sha": sha,
                "gross_added": result.gross_added,
                "net_added": result.net_added,
                "value": result.value,
            }
            for number, sha, result in entries
        ]

        rework = churn.main_rework(
            repo_path,
            day,
            churn_cfg.rework_window_days,
            exclude_commits=churn_cfg.exclude_commits,
            exclude_paths=churn_cfg.exclude_paths,
        )
        per_day[day] = {
            "branch_churn": branch_block,
            "main_rework": {
                "young_removed": rework.young_removed,
                "added_to_main": rework.added_to_main,
                "window_days": rework.window_days,
                "value": rework.value,
            },
            "escalations": escalations_by_day[day],
            "rounds": rounds.summarize([rounds_by_day[day]]),
            "regression_chains": regression_chains_by_day[day],
            "throughput": throughput.summarize([throughput_by_day[day]]),
            "clarification": clarification_by_day[day],
        }

    return per_day, rounds_by_day, throughput_by_day


def run_collect(
    *, since: date, until: date, out_dir: Path, cache_dir: Path, config_dir: Path
) -> None:
    repos = config.load_repos(config_dir / "repos.yml")
    churn_cfg = config.load_churn_config(config_dir / "churn.yml")
    token = os.environ.get("ECOSYSTEM_TOKEN") or os.environ.get("GITHUB_TOKEN")
    days = _daterange(since, until)

    client = github.make_client(token)
    try:
        per_repo_by_day: dict[date, dict[str, dict]] = {day: {} for day in days}
        rounds_samples_by_day: dict[date, list[rounds.DaySamples]] = {day: [] for day in days}
        throughput_samples_by_day: dict[date, list[throughput.DayThroughput]] = {
            day: [] for day in days
        }
        for repo_cfg in repos:
            full_name = f"{repo_cfg.owner}/{repo_cfg.name}"
            per_day, repo_rounds_by_day, repo_throughput_by_day = _collect_repo(
                repo_cfg,
                days=days,
                cache_dir=cache_dir,
                churn_cfg=churn_cfg,
                client=client,
                token=token,
            )
            for day, blocks in per_day.items():
                per_repo_by_day[day][full_name] = blocks
                rounds_samples_by_day[day].append(repo_rounds_by_day[day])
                throughput_samples_by_day[day].append(repo_throughput_by_day[day])
    finally:
        client.close()

    for day in days:
        per_repo = per_repo_by_day[day]
        branch_blocks = [entry["branch_churn"] for entry in per_repo.values()]
        rework_blocks = [entry["main_rework"] for entry in per_repo.values()]
        escalation_blocks = [entry["escalations"] for entry in per_repo.values()]
        regression_chain_blocks = [entry["regression_chains"] for entry in per_repo.values()]
        clarification_blocks = [entry["clarification"] for entry in per_repo.values()]
        payload = {
            "schema_version": 1,
            "date": day.isoformat(),
            "totals": {
                "branch_churn": _merge_branch_churn(branch_blocks),
                "main_rework": _merge_main_rework(
                    rework_blocks, window_days=churn_cfg.rework_window_days
                ),
                "escalations": _merge_escalations(escalation_blocks),
                "rounds": rounds.summarize(rounds_samples_by_day[day]),
                "regression_chains": _merge_regression_chains(regression_chain_blocks),
                "throughput": throughput.summarize(throughput_samples_by_day[day]),
                "clarification": _merge_clarification(clarification_blocks),
            },
            "per_repo": per_repo,
        }
        output.write_daily(out_dir, day.isoformat(), payload)

    output.rebuild_index(out_dir)
