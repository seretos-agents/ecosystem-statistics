"""Plan #29 -- collect over a `repos.yml` that names whole orgs (`orgs:` / `exclude:`).

Offline: `file://` clones of synthetic origins and a `MockTransport` in place of
`ecosystem_statistics.github.make_client`. The `/orgs/{org}/repos` listing is two pages
per org (Link rel=next). Repos that must NOT be collected (excluded, created after
`until`, the listing's copy of an explicitly configured repo) carry an unclonable URL, so a
wrong implementation fails loudly or shows up in `per_repo`.

Expected RED reason: `load_repos` requires a top-level `owner:` (KeyError) / ignores `orgs:`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, read_tree

BOGUS = "file:///nonexistent/bogus.git"
WHEN = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _write_config(config_dir: Path, repos_yml: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "repos.yml").write_text(repos_yml, encoding="utf-8")
    (config_dir / "churn.yml").write_text(
        "rework_window_days: 21\nexclude_paths:\n" '  - "*.lock"\n' "exclude_commits: []\n",
        encoding="utf-8",
    )


def _origin(tmp_path: Path, name: str) -> SyntheticRepo:
    origin = SyntheticRepo(tmp_path / f"{name}-origin")
    origin.commit_file(f"{name}.py", f"{name}\n", f"init {name}", when=WHEN)
    return origin


def _item(name: str, created_at: str, clone_url: str) -> dict:
    return {"name": name, "created_at": created_at, "clone_url": clone_url}


def _argv(out: Path, cache: Path) -> list[str]:
    return [
        "collect",
        "--since", "2024-01-01",
        "--until", "2024-01-03",
        "--out", str(out),
        "--cache-dir", str(cache),
    ]


def _install_client(monkeypatch: pytest.MonkeyPatch, listing_handler) -> list[httpx.URL]:
    import ecosystem_statistics.github as github

    seen: list[httpx.URL] = []

    def make_client(token: str | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            if request.url.path.startswith("/orgs/"):
                return listing_handler(request)
            return httpx.Response(200, json=[])  # /issues, /pulls

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(github, "make_client", make_client)
    return seen


def _paged(page1: list[dict], page2: list[dict], org: str):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=page2)
        next_url = f"https://api.github.com/orgs/{org}/repos?type=all&per_page=100&page=2"
        return httpx.Response(200, json=page1, headers={"Link": f'<{next_url}>; rel="next"'})

    return respond


def test_collect_includes_discovered_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origins = {
        n: _origin(tmp_path, n)
        for n in ("agents-p1", "agents-p2", "mid-x", "games-p1", "games-p2", "dup-x", "explicit-x")
    }
    workdir = tmp_path / "workspace"
    _write_config(
        workdir / "config",
        "orgs: [seretos-agents, seretos-games]\n"
        "exclude: [seretos-agents/excluded-x]\n"
        "repos:\n"
        "  - name: explicit-x\n"
        "    owner: seretos-agents\n"
        f"    clone_url: {origins['explicit-x'].clone_url()}\n"
        "  - name: dup-x\n"
        "    owner: seretos-agents\n"
        f"    clone_url: {origins['dup-x'].clone_url()}\n",
    )
    monkeypatch.chdir(workdir)

    listings = {
        "seretos-agents": _paged(
            [
                _item("agents-p1", "2023-01-01T00:00:00Z", origins["agents-p1"].clone_url()),
                _item("dup-x", "2023-01-01T00:00:00Z", BOGUS),  # explicit entry wins
                _item("excluded-x", "2023-01-01T00:00:00Z", BOGUS),  # exclude: removes it
            ],
            [
                # created inside the window (after `since`, before `until`): kept only if
                # the listing is cut off at `until`, not at `since`
                _item("mid-x", "2024-01-02T10:00:00Z", origins["mid-x"].clone_url()),
                # created on `until` itself (last second): kept
                _item("agents-p2", "2024-01-03T23:59:59Z", origins["agents-p2"].clone_url()),
                # created the day after `until`: dropped
                _item("future-x", "2024-01-04T00:00:00Z", BOGUS),
            ],
            "seretos-agents",
        ),
        "seretos-games": _paged(
            [_item("games-p1", "2023-01-01T00:00:00Z", origins["games-p1"].clone_url())],
            [_item("games-p2", "2023-01-01T00:00:00Z", origins["games-p2"].clone_url())],
            "seretos-games",
        ),
    }
    seen = _install_client(
        monkeypatch, lambda req: listings[req.url.path.split("/")[2]](req)
    )

    from ecosystem_statistics.__main__ import main

    out_a, cache_a = workdir / "out_a", workdir / "cache_a"
    out_b, cache_b = workdir / "out_b", workdir / "cache_b"
    assert main(_argv(out_a, cache_a)) == 0

    payload = json.loads((out_a / "daily" / "2024-01-01.json").read_text(encoding="utf-8"))
    assert set(payload["per_repo"]) == {
        "seretos-agents/agents-p1",
        "seretos-agents/agents-p2",
        "seretos-agents/dup-x",
        "seretos-agents/mid-x",
        "seretos-agents/explicit-x",
        "seretos-games/games-p1",
        "seretos-games/games-p2",
    }

    listing_calls = [u for u in seen if u.path.startswith("/orgs/")]
    for org in ("seretos-agents", "seretos-games"):
        mine = [u for u in listing_calls if u.path == f"/orgs/{org}/repos"]
        assert [u.params.get("page") for u in mine][-1] == "2", (org, mine)
        assert len(mine) >= 2, (org, mine)
        assert mine[0].params["type"] == "all"

    # Byte-identical: fresh output/cache, and a rerun into the same output.
    assert main(_argv(out_b, cache_b)) == 0
    snapshot = read_tree(out_a)
    assert snapshot == read_tree(out_b)
    assert main(_argv(out_a, cache_a)) == 0
    assert read_tree(out_a) == snapshot


@pytest.mark.parametrize("status", [403, 404])
def test_org_listing_failure_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    ok = _origin(tmp_path, "agents-p1")
    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", "orgs: [seretos-agents, seretos-games]\n")
    monkeypatch.chdir(workdir)

    def listing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orgs/seretos-games/repos":
            return httpx.Response(status, json={"message": "nope"})
        return httpx.Response(
            200, json=[_item("agents-p1", "2023-01-01T00:00:00Z", ok.clone_url())]
        )

    _install_client(monkeypatch, listing)

    from ecosystem_statistics.__main__ import main

    out = workdir / "out"
    with pytest.raises(httpx.HTTPStatusError):
        main(_argv(out, workdir / "cache"))

    assert not list(out.glob("daily/*.json"))
