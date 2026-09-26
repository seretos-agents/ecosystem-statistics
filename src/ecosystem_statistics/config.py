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
    """One repo to collect. `clone_url` defaults to the public GitHub URL for
    `owner/name` but can be overridden per-entry (used by tests to point at a local
    `file://` clone instead of the network)."""

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


def load_repos(path: Path) -> list[RepoConfig]:
    """Parse `repos.yml`. Each entry under `repos:` is either a plain repo name string
    (using the default clone URL) or a mapping with `name` and an optional `clone_url`
    override."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    owner = data["owner"]
    repos: list[RepoConfig] = []
    for entry in data.get("repos", []):
        if isinstance(entry, str):
            name = entry
            clone_url = None
        else:
            name = entry["name"]
            clone_url = entry.get("clone_url")
        repos.append(
            RepoConfig(
                owner=owner,
                name=name,
                clone_url=clone_url or _default_clone_url(owner, name),
            )
        )
    return repos


def load_churn_config(path: Path) -> ChurnConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ChurnConfig(
        rework_window_days=int(data.get("rework_window_days", 21)),
        exclude_paths=tuple(data.get("exclude_paths") or []),
        exclude_commits=tuple(data.get("exclude_commits") or []),
    )
