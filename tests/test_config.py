"""R5 — repo scope loads from config, not code (plan #9; see AGENTS.md: "Repo scope lives
in `config/repos.yml`, not in code."). Plan #29 adds org-level discovery (`orgs:`/`exclude:`)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

REPOS_YML = Path(__file__).resolve().parents[1] / "config" / "repos.yml"


def test_repos_yml_scopes_both_orgs() -> None:
    from ecosystem_statistics.config import load_repos

    asked: list[str] = []

    def discover(org: str) -> list[tuple[str, str]]:
        asked.append(org)
        names = ["ecosystem-statistics", "agent-gamejam", "mmo-server"]
        return [(n, f"https://github.com/{org}/{n}.git") for n in names]

    repos = load_repos(REPOS_YML, discover=discover)
    full = {f"{r.owner}/{r.name}" for r in repos}

    assert sorted(asked) == ["seretos-agents", "seretos-games"]
    assert "seretos-games/mmo-server" in full
    assert "seretos-agents/mmo-server" in full
    assert "seretos-agents/ecosystem-statistics" not in full
    # Only ecosystem-statistics is excluded (ticket #29), not agent-gamejam.
    assert "seretos-games/agent-gamejam" in full


def test_load_repos_union_dedup_exclude(tmp_path: Path) -> None:
    from ecosystem_statistics.config import load_repos

    cfg = tmp_path / "repos.yml"
    cfg.write_text(
        "orgs: [a, b]\n"
        "exclude: [a/r1, b/r4]\n"
        "repos:\n"
        "  - name: r2\n"
        "    owner: a\n"
        "    clone_url: file:///override\n"
        "  - name: r4\n"
        "    owner: b\n"
        "  - name: r5\n"
        "    owner: c\n",
        encoding="utf-8",
    )
    listing = {
        "a": [("r1", "u-a-r1"), ("r2", "u-a-r2")],
        "b": [("r3", "u-b-r3"), ("r4", "u-b-r4")],
    }

    repos = load_repos(cfg, discover=lambda org: listing[org])

    assert [(r.owner, r.name, r.clone_url) for r in repos] == [
        ("a", "r2", "file:///override"),
        ("b", "r3", "u-b-r3"),
        ("c", "r5", "https://github.com/c/r5.git"),
    ]


def test_load_repos_orgs_without_discover_raises(tmp_path: Path) -> None:
    from ecosystem_statistics.config import load_repos

    cfg = tmp_path / "repos.yml"
    cfg.write_text("orgs: [a]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_repos(cfg)


def test_load_repos_bare_string_without_owner_raises(tmp_path: Path) -> None:
    from ecosystem_statistics.config import load_repos

    cfg = tmp_path / "repos.yml"
    cfg.write_text("repos:\n  - x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_repos(cfg)


def test_list_org_repos_paginates_and_drops_future_repos() -> None:
    from ecosystem_statistics.github import list_org_repos

    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        assert request.url.path == "/orgs/o/repos"
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json=[
                    {"name": "late", "created_at": "2024-01-02T00:00:00Z", "clone_url": "u-late"},
                    {"name": "edge", "created_at": "2024-01-01T23:59:59Z", "clone_url": "u-edge"},
                ],
            )
        return httpx.Response(
            200,
            json=[
                {"name": "zeta", "created_at": "2023-05-01T00:00:00Z", "clone_url": "u-zeta"},
                {"name": "alpha", "created_at": "2023-06-01T00:00:00Z", "clone_url": "u-alpha"},
            ],
            headers={
                "Link": '<https://api.github.com/orgs/o/repos?type=all&per_page=100&page=2>; rel="next"'
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = list_org_repos(client, "o", until=date(2024, 1, 1))

    assert result == [("alpha", "u-alpha"), ("edge", "u-edge"), ("zeta", "u-zeta")]
    assert len(seen) == 2
    assert seen[0].params["type"] == "all"


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
