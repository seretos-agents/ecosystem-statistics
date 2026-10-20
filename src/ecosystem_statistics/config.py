"""Loads repo scope and churn tuning from YAML config files.

Per AGENTS.md: "Repo scope lives in `config/repos.yml`, not in code." This module is the
only place that reads `config/repos.yml` and `config/churn.yml`; nothing else hardcodes
repo names or the rework window.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RepoConfig:
    """One repo to collect.

    `owner` is the entry's own owner or the file's default. `clone_url` defaults to
    the public GitHub URL for `owner/name` but can be overridden per-entry (used by
    tests to point at a local `file://` clone instead of the network)."""

    owner: str
    name: str
    clone_url: str


@dataclass(frozen=True)
class ChurnConfig:
    rework_window_days: int
    exclude_paths: tuple[str, ...]
    exclude_commits: tuple[str, ...]


def _default_clone_url(owner: str, name: str) -> str:
    return f"https://github.com/{owner}/{name}.git"


def load_repos(path: Path, *, discover=None) -> list[RepoConfig]:
    """Parse `repos.yml`. Optional keys:

    - `owner`: default owner for bare-string entries (required only if one is used).
    - `repos`: each entry is a name string or a mapping with `name` and optional `owner`
      and `clone_url`.
    - `orgs`: org names whose every repo is collected, via `discover(org) -> [(name,
      clone_url)]` (injected so this module stays free of HTTP).
    - `exclude`: `owner/name` entries dropped from the result, discovered or explicit.

    An explicit entry wins over a discovered repo with the same `owner/name`. The result
    is sorted by `(owner, name)`."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    default_owner = data.get("owner")
    orgs = data.get("orgs") or []
    if orgs and discover is None:
        raise ValueError("repos.yml lists `orgs:` but no discover callable was given")

    found: dict[tuple[str, str], RepoConfig] = {}
    for org in orgs:
        for name, clone_url in discover(org):
            found[(org, name)] = RepoConfig(owner=org, name=name, clone_url=clone_url)

    for entry in data.get("repos") or []:
        if isinstance(entry, str):
            name = entry
            owner = default_owner
            clone_url = None
        else:
            name = entry["name"]
            owner = entry.get("owner", default_owner)
            clone_url = entry.get("clone_url")
        if owner is None:
            raise ValueError(f"repo entry {name!r} has no owner and repos.yml has no `owner:`")
        found[(owner, name)] = RepoConfig(
            owner=owner,
            name=name,
            clone_url=clone_url or _default_clone_url(owner, name),
        )

    excluded = set(data.get("exclude") or [])
    return [
        found[key] for key in sorted(found) if f"{key[0]}/{key[1]}" not in excluded
    ]


def load_churn_config(path: Path) -> ChurnConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ChurnConfig(
        rework_window_days=int(data.get("rework_window_days", 21)),
        exclude_paths=tuple(data.get("exclude_paths") or []),
        exclude_commits=tuple(data.get("exclude_commits") or []),
    )
