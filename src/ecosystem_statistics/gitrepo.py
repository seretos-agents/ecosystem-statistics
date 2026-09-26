"""Thin wrapper around the `git` CLI.

Every invocation pins `core.autocrlf=false -c core.quotepath=off` (plan #9) so rename/edit
byte counts and blame dates don't depend on the invoking machine's global git config. Read
commands (`show`, `diff`, `blame`, `log`, `rev-list`, `rev-parse`, `symbolic-ref`) work
identically against a bare clone -- this module never needs a working tree.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

# The canonical empty-tree object every git repo has -- used as the "parent" of a root
# commit so `git diff`/`git show` can be run uniformly even when there is no real C^1.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_BASE_CONFIG = ["-c", "core.autocrlf=false", "-c", "core.quotepath=off"]


def run(cwd: Path, *args: str) -> str:
    """Run a git command scoped to `cwd` and return stdout as text."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *_BASE_CONFIG, *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def run_bytes(cwd: Path, *args: str) -> bytes:
    """Run a git command scoped to `cwd` and return raw stdout bytes.

    Used for NUL-separated `--numstat -z` output, where text-mode newline translation
    must never touch the byte stream.
    """
    result = subprocess.run(
        ["git", "-C", str(cwd), *_BASE_CONFIG, *args],
        check=True,
        capture_output=True,
    )
    return result.stdout


def _auth_header_args(token: str | None) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def clone_bare(clone_url: str, target: Path, *, token: str | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            *_auth_header_args(token),
            *_BASE_CONFIG,
            "clone",
            "--bare",
            clone_url,
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def fetch_all(target: Path, *, token: str | None = None) -> None:
    subprocess.run(
        [
            "git",
            *_auth_header_args(token),
            "-C",
            str(target),
            *_BASE_CONFIG,
            "fetch",
            "--prune",
            "origin",
            "+refs/heads/*:refs/heads/*",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def ensure_repo(
    clone_url: str, cache_dir: Path, owner: str, name: str, *, token: str | None = None
) -> Path:
    """Bare-clone `owner/name` into `<cache_dir>/<owner>/<name>` if it isn't already
    there, else fetch updates. Never `--depth`-limited (plan #9: full history is needed
    for main_rework's backward-looking blame)."""
    target = Path(cache_dir) / owner / name
    if (target / "HEAD").exists():
        fetch_all(target, token=token)
    else:
        clone_bare(clone_url, target, token=token)
    return target


def default_branch(repo_path: Path) -> str:
    """The branch HEAD points at in a bare clone -- whatever the remote's default branch
    was at clone time (plan #9: "main line = HEAD")."""
    ref = run(repo_path, "symbolic-ref", "HEAD").strip()
    return ref.rsplit("/", 1)[-1]


def rev_parents(repo_path: Path, sha: str) -> list[str]:
    """The parent SHAs of `sha`, in order. Empty for a root commit."""
    line = run(repo_path, "rev-list", "--parents", "-n", "1", sha).strip()
    tokens = line.split()
    return tokens[1:]
