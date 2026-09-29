"""R5 — repo scope loads from config, not code (plan #9; see AGENTS.md: "Repo scope lives
in `config/repos.yml`, not in code.")."""

from __future__ import annotations

from pathlib import Path

REPOS_YML = Path(__file__).resolve().parents[1] / "config" / "repos.yml"


def test_repos_yml_lists_25_repos_under_two_owners() -> None:
    from ecosystem_statistics.config import load_repos

    repos = load_repos(REPOS_YML)

    assert len(repos) == 25
    assert {r.owner for r in repos} == {"seretos-agents", "seretos-games"}
    agents = [r for r in repos if r.owner == "seretos-agents"]
    games = [r for r in repos if r.owner == "seretos-games"]
    assert len(agents) == 20
    assert {r.name for r in games} == {
        "unity-fps-controls",
        "unity-interaction",
        "unity-avatar",
        "unity-menu",
        "basic-fps",
    }
    assert "agent-gamejam" not in {r.name for r in repos}
    # Default clone URL follows each entry's own owner.
    for r in agents:
        assert r.clone_url == f"https://github.com/seretos-agents/{r.name}.git"
    for r in games:
        assert r.clone_url == f"https://github.com/seretos-games/{r.name}.git"


def test_mixed_owner_entries(tmp_path: Path) -> None:
    from ecosystem_statistics.config import load_repos

    cfg = tmp_path / "repos.yml"
    cfg.write_text(
        "owner: a\n"
        "repos:\n"
        "  - x\n"
        "  - name: y\n"
        "    owner: b\n"
        "  - name: z\n"
        "    owner: b\n"
        "    clone_url: file:///z\n",
        encoding="utf-8",
    )

    repos = load_repos(cfg)

    assert [(r.owner, r.name, r.clone_url) for r in repos] == [
        ("a", "x", "https://github.com/a/x.git"),
        ("b", "y", "https://github.com/b/y.git"),
        ("b", "z", "file:///z"),
    ]
