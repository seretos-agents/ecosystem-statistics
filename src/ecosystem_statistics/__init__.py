"""Deterministic nightly collector of Modular Software Factory ecosystem statistics.

This package is a plain, deterministic collector — it never starts an LLM session and
never calls a Claude Code / Codex CLI. It reads GitHub (issues, comments, PRs, commits)
across the configured repos and writes daily JSON snapshots to the `data` branch.

The concrete collection, parsing and metrics logic lands ticket by ticket; this module
only re-exports the package version.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
