"""Orchestrates one `collect` run: clone/fetch each configured repo, compute branch_churn
(per merged PR) and main_rework (per UTC day) for every day in `[since, until]`, and write
deterministic daily JSON files plus a cumulative index (plan #9)."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

from . import churn, config, github, gitrepo, output


def _daterange(since: date, until: date) -> list[date]:
    days = []
    day = since
    while day <= until:
        days.append(day)
        day += timedelta(days=1)
    return days


def _round4(value: float) -> float:
    return round(value, 4)


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


def _collect_repo(
    repo_cfg: config.RepoConfig,
    *,
    days: list[date],
    cache_dir: Path,
    churn_cfg: config.ChurnConfig,
    client,
    token: str | None,
) -> dict[date, dict]:
    """branch_churn + main_rework blocks for one repo, for every day in `days`."""
    repo_path = gitrepo.ensure_repo(
        repo_cfg.clone_url, cache_dir, repo_cfg.owner, repo_cfg.name, token=token
    )
    full_name = f"{repo_cfg.owner}/{repo_cfg.name}"
    default_branch = gitrepo.default_branch(repo_path)

    prs = github.list_merged_prs(
        client, repo_cfg.owner, repo_cfg.name, default_branch, days[0], days[-1]
    )
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
        }

    return per_day


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
        for repo_cfg in repos:
            full_name = f"{repo_cfg.owner}/{repo_cfg.name}"
            per_day = _collect_repo(
                repo_cfg,
                days=days,
                cache_dir=cache_dir,
                churn_cfg=churn_cfg,
                client=client,
                token=token,
            )
            for day, blocks in per_day.items():
                per_repo_by_day[day][full_name] = blocks
    finally:
        client.close()

    for day in days:
        per_repo = per_repo_by_day[day]
        branch_blocks = [entry["branch_churn"] for entry in per_repo.values()]
        rework_blocks = [entry["main_rework"] for entry in per_repo.values()]
        payload = {
            "schema_version": 1,
            "date": day.isoformat(),
            "totals": {
                "branch_churn": _merge_branch_churn(branch_blocks),
                "main_rework": _merge_main_rework(
                    rework_blocks, window_days=churn_cfg.rework_window_days
                ),
            },
            "per_repo": per_repo,
        }
        output.write_daily(out_dir, day.isoformat(), payload)

    output.rebuild_index(out_dir)
