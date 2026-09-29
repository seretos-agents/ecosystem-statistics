"""R1 (plan #26) -- a collect run over a `repos.yml` naming repos under two owners.

The top-level `owner:` stays the default; an entry may carry its own `owner:`. Offline:
`file://` clones of two synthetic repos and a `MockTransport` in place of
`ecosystem_statistics.github.make_client`, recording every request path.

Expected RED reason: `load_repos` ignores the entry-level `owner`, so `per_repo` holds
`seretos-agents/demo-g` instead of `seretos-games/demo-g`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, read_tree


def _write_config(config_dir: Path, agents_url: str, games_url: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "repos.yml").write_text(
        "owner: seretos-agents\n"
        "repos:\n"
        "  - name: demo-a\n"
        f"    clone_url: {agents_url}\n"
        "  - name: demo-g\n"
        "    owner: seretos-games\n"
        f"    clone_url: {games_url}\n",
        encoding="utf-8",
    )
    (config_dir / "churn.yml").write_text(
        "rework_window_days: 21\n"
        "exclude_paths:\n"
        '  - "*.lock"\n'
        "exclude_commits: []\n",
        encoding="utf-8",
    )


def test_two_owners_collected_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    agents_origin = SyntheticRepo(tmp_path / "agents-origin")
    agents_origin.commit_file("a.py", "a\n", "init a", when=when)
    games_origin = SyntheticRepo(tmp_path / "games-origin")
    games_origin.commit_file("g.py", "g\n", "init g", when=when)

    workdir = tmp_path / "workspace"
    _write_config(workdir / "config", agents_origin.clone_url(), games_origin.clone_url())
    monkeypatch.chdir(workdir)

    from ecosystem_statistics.__main__ import main
    import ecosystem_statistics.github as github

    seen_paths: list[str] = []

    def make_recording_client(token: str | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen_paths.append(path)
            if path.endswith("/issues"):
                # One open issue per repo so the per-item comments call is exercised too.
                return httpx.Response(
                    200,
                    json=[{"number": 1, "created_at": "2024-01-01T08:00:00Z", "closed_at": None}],
                )
            if path.endswith("/issues/1/comments"):
                return httpx.Response(200, json=[])
            assert path.endswith("/pulls"), path
            return httpx.Response(200, json=[])

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(github, "make_client", make_recording_client)

    def argv(out: Path, cache: Path) -> list[str]:
        return [
            "collect",
            "--since", "2024-01-01",
            "--until", "2024-01-01",
            "--out", str(out),
            "--cache-dir", str(cache),
        ]

    out_a, cache_a = workdir / "out_a", workdir / "cache_a"
    out_b, cache_b = workdir / "out_b", workdir / "cache_b"
    assert main(argv(out_a, cache_a)) == 0

    payload = json.loads((out_a / "daily" / "2024-01-01.json").read_text(encoding="utf-8"))
    assert set(payload["per_repo"]) == {"seretos-agents/demo-a", "seretos-games/demo-g"}
    for block in payload["per_repo"].values():
        assert set(block) == set(payload["totals"])

    # API routing: every repo-scoped request goes to that repo's own owner, for both repos,
    # covering list endpoints and the per-item comments call.
    for owner, name in (("seretos-agents", "demo-a"), ("seretos-games", "demo-g")):
        prefix = f"/repos/{owner}/{name}/"
        mine = [p for p in seen_paths if p.startswith(prefix)]
        assert any(p.endswith("/issues") for p in mine), (owner, name, seen_paths)
        assert any(p.endswith("/pulls") for p in mine), (owner, name, seen_paths)
        assert any(p.endswith("/issues/1/comments") for p in mine), (owner, name, seen_paths)
    assert not [p for p in seen_paths if "/repos/seretos-agents/demo-g/" in p]
    assert not [p for p in seen_paths if "/repos/seretos-games/demo-a/" in p]
    assert len(seen_paths) == len(
        [p for p in seen_paths if p.startswith(("/repos/seretos-agents/demo-a/", "/repos/seretos-games/demo-g/"))]
    )

    assert (cache_a / "seretos-games" / "demo-g").exists()
    assert (cache_a / "seretos-agents" / "demo-a").exists()

    # Byte-identical: fresh output/cache, and a rerun into the same output.
    assert main(argv(out_b, cache_b)) == 0
    snapshot_a = read_tree(out_a)
    assert snapshot_a == read_tree(out_b)
    assert main(argv(out_a, cache_a)) == 0
    assert read_tree(out_a) == snapshot_a
