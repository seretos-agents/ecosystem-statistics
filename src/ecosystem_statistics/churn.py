"""The two churn metrics (plan #9).

`branch_churn(repo, merge_sha)` — per merged PR, how much of the feature branch's own
commit-by-commit line growth (`gross_added`) didn't survive to the final merge diff
(`net_added`): `1 - net_added/gross_added`. High values mean critique/fixups rewrote code
before it ever reached main.

`main_rework(repo, day, window_days)` — for a UTC day D, how many lines removed from main
on that day were themselves added to main within the last `window_days` days (backward
window `W(D)`), divided by how many lines were added to main in that same window. High
values mean code that just landed on main had to be reworked soon after.

Every diff/numstat call passes `-M50% -l32767` so rename detection is independent of the
invoking machine's git version/config (plan #9, F1) -- a pure rename must not look like a
100-line delete plus a 100-line add.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from posixpath import basename

from . import gitrepo

_RENAME_FLAGS = ("-M50%", "-l32767")


# ---------------------------------------------------------------------------
# numstat -z parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NumstatEntry:
    added: int | None  # None means binary ("-" column)
    old_path: str
    new_path: str


def _unquote_path(raw: str) -> str:
    # `core.quotepath=off` (always set by gitrepo) already keeps git from C-quoting
    # non-ASCII paths; this is defensive for the rare literal-quote-character path.
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    return raw


def _parse_numstat_z(raw: bytes) -> list[_NumstatEntry]:
    """Parse `git diff/show --numstat -z` output.

    A plain record is `added\\tdeleted\\tpath\\0`. A rename/copy record is
    `added\\tdeleted\\t\\0old\\0new\\0` -- the tab-separated head has an empty path field,
    and the actual old/new paths follow as their own NUL-terminated fields.
    """
    text = raw.decode("utf-8")
    if not text:
        return []
    fields = text.split("\x00")
    if fields and fields[-1] == "":
        fields.pop()

    entries: list[_NumstatEntry] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        added_str, deleted_str, tail = record.split("\t", 2)
        added = None if added_str == "-" else int(added_str)
        if tail == "":
            old_path = _unquote_path(fields[i + 1])
            new_path = _unquote_path(fields[i + 2])
            i += 3
        else:
            old_path = new_path = _unquote_path(tail)
            i += 1
        entries.append(_NumstatEntry(added=added, old_path=old_path, new_path=new_path))
    return entries


def _is_excluded(path: str, exclude_paths: Sequence[str]) -> bool:
    name = basename(path)
    return any(fnmatch(path, pattern) or fnmatch(name, pattern) for pattern in exclude_paths)


def _sum_added(entries: Iterable[_NumstatEntry], exclude_paths: Sequence[str]) -> int:
    total = 0
    for entry in entries:
        if entry.added is None:
            continue  # binary file, no line counts
        if _is_excluded(entry.old_path, exclude_paths) or _is_excluded(
            entry.new_path, exclude_paths
        ):
            continue
        total += entry.added
    return total


def _round4(value: float) -> float:
    return round(value, 4)


# ---------------------------------------------------------------------------
# R1 -- branch_churn
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchChurnResult:
    gross_added: int
    net_added: int
    value: float | None


def branch_churn(
    repo_path: Path, merge_sha: str, exclude_paths: Sequence[str] = ()
) -> BranchChurnResult:
    parents = gitrepo.rev_parents(repo_path, merge_sha)
    if len(parents) != 2:
        raise ValueError(f"{merge_sha} is not a two-parent merge commit")
    first_parent, second_parent = parents

    commit_list = gitrepo.run(
        repo_path, "rev-list", "--no-merges", f"{first_parent}..{second_parent}"
    ).split()

    gross_added = 0
    for commit in commit_list:
        raw = gitrepo.run_bytes(
            repo_path,
            "show",
            "--numstat",
            "-z",
            "--format=",
            *_RENAME_FLAGS,
            commit,
        )
        gross_added += _sum_added(_parse_numstat_z(raw), exclude_paths)

    raw_net = gitrepo.run_bytes(
        repo_path,
        "diff",
        "--numstat",
        "-z",
        *_RENAME_FLAGS,
        first_parent,
        second_parent,
    )
    net_added = _sum_added(_parse_numstat_z(raw_net), exclude_paths)

    value = None if gross_added == 0 else _round4(1 - net_added / gross_added)
    return BranchChurnResult(gross_added=gross_added, net_added=net_added, value=value)


# ---------------------------------------------------------------------------
# R2/R6 -- main_rework
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MainReworkResult:
    young_removed: int
    added_to_main: int
    window_days: int
    value: float | None


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_OLD_PATH_RE = re.compile(r"^--- (?:a/(?P<path>.*)|/dev/null)$")
_BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40}) \d+ \d+")


def _window_bounds(day: date, window_days: int) -> tuple[datetime, datetime]:
    """W(D) = (end(D) - window_days, end(D)], where end(D) is midnight UTC starting the
    day after D."""
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    start = end - timedelta(days=window_days)
    return start, end


def _in_window(ts: datetime, bounds: tuple[datetime, datetime]) -> bool:
    start, end = bounds
    return start < ts <= end


def _sha_excluded(sha: str, exclude_commits: Sequence[str]) -> bool:
    return any(sha == pattern or sha.startswith(pattern) for pattern in exclude_commits)


def _first_parent_commits(repo_path: Path) -> list[tuple[str, datetime]]:
    """Every first-parent commit reachable from HEAD, oldest git-log order, as
    (sha, committer-time-UTC). Committer time from `%ct` is already an absolute UTC
    epoch second, so no timezone-offset arithmetic is needed to bucket it by UTC day."""
    out = gitrepo.run(repo_path, "log", "--first-parent", "--format=%H %ct").strip()
    commits: list[tuple[str, datetime]] = []
    if out:
        for line in out.splitlines():
            sha, ct = line.split()
            commits.append((sha, datetime.fromtimestamp(int(ct), tz=timezone.utc)))
    return commits


def _diff_base(repo_path: Path, sha: str) -> str:
    """C^1 normally; the empty tree for a root commit, so root-vs-empty-tree diffs use
    the same code path as any other commit."""
    parents = gitrepo.rev_parents(repo_path, sha)
    return parents[0] if parents else gitrepo.EMPTY_TREE_SHA


def _added_lines(repo_path: Path, base: str, sha: str, exclude_paths: Sequence[str]) -> int:
    raw = gitrepo.run_bytes(
        repo_path, "diff", "--numstat", "-z", *_RENAME_FLAGS, base, sha
    )
    return _sum_added(_parse_numstat_z(raw), exclude_paths)


def _removed_ranges(diff_text: str) -> list[tuple[str, int, int]]:
    """(old_path, first_line, last_line) 1-based inclusive ranges of lines a `-U0` diff
    removed from their old-side path, keyed off each file block's `--- a/<old path>`."""
    ranges: list[tuple[str, int, int]] = []
    old_path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            match = _OLD_PATH_RE.match(line)
            raw_path = match.group("path") if match else None
            old_path = _unquote_path(raw_path) if raw_path else None
        elif line.startswith("@@ "):
            match = _HUNK_RE.match(line)
            if not match or old_path is None:
                continue
            start = int(match.group(1))
            length = int(match.group(2)) if match.group(2) is not None else 1
            if length == 0:
                continue  # pure insertion hunk -- nothing removed on the old side
            ranges.append((old_path, start, start + length - 1))
    return ranges


def _blame_origins(
    repo_path: Path, rev: str, old_path: str, start: int, end: int
) -> list[tuple[str, datetime]]:
    """The (origin commit sha, origin committer-time UTC) of each line in `[start, end]`
    of `old_path` as of `rev`. `git blame` follows renames on its own, so a line moved by
    an earlier `git mv` still reports the commit that actually introduced its content."""
    raw = gitrepo.run_bytes(
        repo_path,
        "blame",
        "--first-parent",
        "--porcelain",
        "-L",
        f"{start},{end}",
        rev,
        "--",
        old_path,
    )
    text = raw.decode("utf-8")
    committer_time: dict[str, int] = {}
    origins: list[tuple[str, datetime]] = []
    current_sha: str | None = None
    for line in text.splitlines():
        header = _BLAME_HEADER_RE.match(line)
        if header:
            current_sha = header.group(1)
            continue
        if line.startswith("committer-time ") and current_sha is not None:
            committer_time[current_sha] = int(line.split(" ", 1)[1])
            continue
        if line.startswith("\t") and current_sha is not None:
            origins.append(
                (current_sha, datetime.fromtimestamp(committer_time[current_sha], tz=timezone.utc))
            )
    return origins


def main_rework(
    repo_path: Path,
    day: date,
    window_days: int,
    exclude_commits: Sequence[str] = (),
    exclude_paths: Sequence[str] = (),
) -> MainReworkResult:
    bounds = _window_bounds(day, window_days)
    commits = _first_parent_commits(repo_path)

    young_removed = 0
    for sha, ts in commits:
        if ts.date() != day or _sha_excluded(sha, exclude_commits):
            continue
        base = _diff_base(repo_path, sha)
        diff_text = gitrepo.run(repo_path, "diff", "-U0", *_RENAME_FLAGS, base, sha)
        for old_path, start, end in _removed_ranges(diff_text):
            if _is_excluded(old_path, exclude_paths):
                continue
            for origin_sha, origin_ts in _blame_origins(repo_path, base, old_path, start, end):
                if _sha_excluded(origin_sha, exclude_commits):
                    continue
                if _in_window(origin_ts, bounds):
                    young_removed += 1

    added_to_main = 0
    for sha, ts in commits:
        if not _in_window(ts, bounds) or _sha_excluded(sha, exclude_commits):
            continue
        base = _diff_base(repo_path, sha)
        added_to_main += _added_lines(repo_path, base, sha, exclude_paths)

    value = None if added_to_main == 0 else _round4(young_removed / added_to_main)
    return MainReworkResult(
        young_removed=young_removed,
        added_to_main=added_to_main,
        window_days=window_days,
        value=value,
    )
