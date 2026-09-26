"""Publish deterministic collector output to a `data` branch on a git remote (plan #10).

`publish()` never touches the code checkout's own working tree: it drives a separate
`git worktree` checked out from `<remote>/<branch>` (creating `branch` as an empty orphan
commit first if it doesn't exist yet), writes into that worktree via the `collect`
callable -- the test seam named in the plan; production wires it to a lambda around
`collect.run_collect`, tests wire it to a fake calling the real `output` writers directly
-- and commits/pushes only when `git status --porcelain` shows a real diff. `main` is
never read from or written to.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .collect import run_collect

# Same reproducibility baseline as gitrepo.py (plan #9): no autocrlf line-ending
# rewriting, no quoted non-ASCII paths, on any invoking machine or CI runner.
_GIT_BASE_ARGS = ["-c", "core.autocrlf=false", "-c", "core.quotepath=off"]

# CI runners have no global git identity; this identity is fixed, not configurable --
# every commit this module makes is a machine-generated data commit, never a human's.
_BOT_IDENTITY_ARGS = [
    "-c", "user.name=github-actions[bot]",
    "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
]

INITIAL_COMMIT_MESSAGE = "Initialize data branch"


@dataclass(frozen=True)
class PublishResult:
    created_branch: bool
    committed: bool
    pushed: bool


def _git(cwd: Path, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run one git command scoped to `cwd`. The sole seam every commit/push/worktree
    mutation in this module runs through -- R3's driving test wraps this exact function
    to prove the no-diff path makes no `commit`/`push` call."""
    run_kwargs: dict[str, object] = {
        "check": True,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
    }
    run_kwargs.update(kwargs)
    return subprocess.run(
        ["git", "-C", str(cwd), *_GIT_BASE_ARGS, *_BOT_IDENTITY_ARGS, *args],
        **run_kwargs,
    )


def _branch_exists(cwd: Path, remote: str, branch: str) -> bool:
    result = _git(cwd, "ls-remote", "--exit-code", "--heads", remote, branch, check=False)
    return result.returncode == 0


def _create_orphan_branch(repo_dir: Path, remote: str, branch: str) -> None:
    """An empty, parentless commit on `branch`, pushed straight to `remote` -- never
    touching any working tree, so a missing `data` branch is never accidentally branched
    off whatever `main` happens to be checked out to."""
    tree_sha = _git(repo_dir, "mktree", input="").stdout.strip()
    commit_sha = _git(repo_dir, "commit-tree", tree_sha, "-m", INITIAL_COMMIT_MESSAGE).stdout.strip()
    _git(repo_dir, "push", remote, f"{commit_sha}:refs/heads/{branch}")


def publish(
    repo_dir: Path,
    *,
    remote: str,
    branch: str,
    worktree_dir: Path,
    message: str,
    collect: Callable[[Path], None],
) -> PublishResult:
    repo_dir = Path(repo_dir)
    worktree_dir = Path(worktree_dir)

    created_branch = not _branch_exists(repo_dir, remote, branch)
    if created_branch:
        _create_orphan_branch(repo_dir, remote, branch)

    # actions/checkout only fetches the default branch (`main`), so `data` -- new or
    # existing -- is always fetched explicitly here.
    _git(repo_dir, "fetch", remote, f"refs/heads/{branch}")
    _git(repo_dir, "worktree", "add", "-B", branch, str(worktree_dir), "FETCH_HEAD")

    try:
        collect(worktree_dir)

        status = _git(worktree_dir, "status", "--porcelain").stdout
        if not status.strip():
            return PublishResult(created_branch=created_branch, committed=False, pushed=False)

        _git(worktree_dir, "add", "-A")
        _git(worktree_dir, "commit", "-m", message)
        _git(worktree_dir, "push", remote, f"HEAD:refs/heads/{branch}")
        return PublishResult(created_branch=created_branch, committed=True, pushed=True)
    finally:
        _git(repo_dir, "worktree", "remove", "--force", str(worktree_dir))


def _utc_today() -> date:
    """The only wall-clock read in this module -- injected as a seam so R4's driving
    test can pin "today" without depending on when the suite happens to run. Never
    lands in any written output."""
    return datetime.now(timezone.utc).date()


def default_window() -> tuple[date, date]:
    """The nightly schedule has no inputs, so its window defaults to yesterday's UTC
    calendar day (both endpoints inclusive, per #9's `_daterange`)."""
    yesterday = _utc_today() - timedelta(days=1)
    return yesterday, yesterday


def _make_collect(*, since: date, until: date, cache_dir: Path, config_dir: Path) -> Callable[[Path], None]:
    def collect(out_dir: Path) -> None:
        run_collect(since=since, until=until, out_dir=out_dir, cache_dir=cache_dir, config_dir=config_dir)

    return collect


def publish_cli(
    *,
    repo_dir: Path,
    remote: str,
    branch: str,
    worktree_dir: Path,
    since: date | None,
    until: date | None,
    cache_dir: Path,
    config_dir: Path,
) -> PublishResult:
    """Entry point for the `publish` CLI subcommand: resolves the default window,
    builds the production `collect` seam around `run_collect`, and delegates to
    `publish()`."""
    if since is None or until is None:
        since, until = default_window()
    message = f"data: collect {since.isoformat()}..{until.isoformat()}"
    collect = _make_collect(since=since, until=until, cache_dir=cache_dir, config_dir=config_dir)
    return publish(
        repo_dir,
        remote=remote,
        branch=branch,
        worktree_dir=worktree_dir,
        message=message,
        collect=collect,
    )
