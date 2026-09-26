"""Shared test fixtures.

The collector's two metrics (branch_churn, main_rework) are computed entirely from git
history via subprocess calls to the real `git` binary. Per AGENTS.md, tests must never
clone a real repo from the network — they build a small, fully-controlled synthetic repo
on disk instead, with pinned author/committer dates so day-bucketing is deterministic
rather than dependent on wall-clock time.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


class SyntheticRepo:
    """A throwaway git repository with a fixed identity, pinned commit dates, and a
    `mv` helper for rename-detection tests (see plan #9, F1)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self._run("init", "-q", "-b", "main")
        self._run("config", "user.name", "Synthetic Committer")
        self._run("config", "user.email", "synthetic@example.invalid")
        self._run("config", "commit.gpgsign", "false")
        self._run("config", "core.autocrlf", "false")
        self._run("config", "core.quotepath", "off")

    # -- low-level -------------------------------------------------------

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env=env,
        )

    @staticmethod
    def _commit_env(when: datetime | None) -> dict[str, str] | None:
        if when is None:
            return None
        stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        return env

    # -- building history --------------------------------------------------

    def write(self, relpath: str, content: str) -> None:
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)

    def commit_file(
        self, relpath: str, content: str, message: str, when: datetime | None = None
    ) -> str:
        self.write(relpath, content)
        return self.commit(message, when=when, paths=[relpath])

    def commit(
        self,
        message: str,
        when: datetime | None = None,
        paths: list[str] | None = None,
    ) -> str:
        self._run("add", *(paths if paths else ["-A"]))
        self._run("commit", "-m", message, env=self._commit_env(when))
        return self.head()

    def mv(self, src: str, dst: str) -> None:
        self._run("mv", src, dst)

    def branch(self, name: str, start_point: str | None = None) -> None:
        args = ["checkout", "-q", "-b", name]
        if start_point:
            args.append(start_point)
        self._run(*args)

    def checkout(self, ref: str) -> None:
        self._run("checkout", "-q", ref)

    def merge(self, ref: str, message: str, when: datetime | None = None) -> str:
        self._run("merge", "--no-ff", "-m", message, ref, env=self._commit_env(when))
        return self.head()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def rev_parse(self, ref: str) -> str:
        return self._run("rev-parse", ref).stdout.strip()

    def clone_url(self) -> str:
        """A `file://` URI usable as a `clone_url` in `config/repos.yml` — the collector
        must clone/fetch like any other remote, never read this working tree directly."""
        return self.path.resolve().as_uri()


@pytest.fixture
def synthetic_repo(tmp_path: Path) -> SyntheticRepo:
    return SyntheticRepo(tmp_path / "origin")


def make_fixup_branch(repo: SyntheticRepo) -> None:
    """Shared branch shape for R1/R6: c1 adds 10 lines, c2 rewrites 4 of them, c3 adds 2
    more, plus an excluded-path lockfile commit. Used by both the plain branch_churn
    driving test and the rename-detection driving test so their expected numbers
    (gross_added=16, net_added=12, value=0.25) stay in one place."""
    ten_lines = "\n".join(f"line{i}" for i in range(10)) + "\n"
    repo.commit_file("feature.py", ten_lines, "c1: add 10 lines")

    rewritten = "\n".join(
        [f"line{i}-rewritten" for i in range(4)] + [f"line{i}" for i in range(4, 10)]
    ) + "\n"
    repo.commit_file("feature.py", rewritten, "c2: rewrite 4 lines")

    final = rewritten.rstrip("\n") + "\nline10\nline11\n"
    repo.commit_file("feature.py", final, "c3: add 2 lines")

    lockfile = "\n".join(f"dep{i}" for i in range(100)) + "\n"
    repo.commit_file("package-lock.json", lockfile, "add lockfile (excluded path)")


def read_tree(root: Path) -> dict[str, bytes]:
    """All files under `root`, keyed by POSIX-style relative path, for byte-for-byte
    comparison across collector runs (AGENTS.md: reruns must be byte-identical)."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
