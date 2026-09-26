"""R5 — repo scope loads from config, not code (plan #9; see AGENTS.md: "Repo scope lives
in `config/repos.yml`, not in code.")."""

from __future__ import annotations

from pathlib import Path

REPOS_YML = Path(__file__).resolve().parents[1] / "config" / "repos.yml"


def test_repos_yml_lists_20_repos() -> None:
    from ecosystem_statistics.config import load_repos

    repos = load_repos(REPOS_YML)

    assert len(repos) == 20
    assert all(r.owner == "seretos-agents" for r in repos)
    assert all(
        r.clone_url == f"https://github.com/{r.owner}/{r.name}.git" for r in repos
    )
