"""R2/R3 (#33) -- a collect run over a repo with no commits must still write the day.

Offline: `file://` clones and a `MockTransport` in place of `github.make_client`.
Expected RED reason: `CalledProcessError` (exit 128, `git log --first-parent` on an
unborn HEAD) propagating out of `main`, so no daily file is written for any repo.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, read_tree

ZERO_MAIN_REWORK = {"young_removed": 0, "added_to_main": 0, "window_days": 21, "value": None}
ZERO_BRANCH_CHURN = {"gross_added": 0, "net_added": 0, "prs": 0, "value": None, "per_pr": []}


def _write_config(config_dir: Path, repos: dict[str, str]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    body = "owner: acme\nrepos:\n"
    for name, url in repos.items():
        body += f"  - name: {name}\n    clone_url: {url}\n"
    (config_dir / "repos.yml").write_text(body, encoding="utf-8")
    (config_dir / "churn.yml").write_text(
        'rework_window_days: 21\nexclude_paths:\n  - "*.lock"\nexclude_commits: []\n',
        encoding="utf-8",
    )


def _client(token: str | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(("/issues", "/pulls")), request.url.path
        return httpx.Response(200, json=[])

    return httpx.Client(transport=httpx.MockTransport(handler))


def _argv(out: Path, cache: Path) -> list[str]:
    return [
        "collect",
        "--since", "2024-01-01",
        "--until", "2024-01-01",
        "--out", str(out),
        "--cache-dir", str(cache),
    ]


def _daily(out: Path) -> dict:
    return json.loads((out / "daily" / "2024-01-01.json").read_text(encoding="utf-8"))


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    demo = SyntheticRepo(tmp_path / "demo-origin")
    demo.commit_file("a.py", "a\n", "init a", when=when)
    empty = SyntheticRepo(tmp_path / "empty-origin")

    workdir = tmp_path / "workspace"
    import ecosystem_statistics.github as github

    monkeypatch.setattr(github, "make_client", _client)
    return demo, empty, workdir


def test_commitless_repo_does_not_abort_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem_statistics.__main__ import main

    demo, empty, workdir = _setup(tmp_path, monkeypatch)

    # Baseline: only the repo with history.
    base_dir = tmp_path / "base"
    _write_config(base_dir / "config", {"demo": demo.clone_url()})
    monkeypatch.chdir(base_dir)
    assert main(_argv(base_dir / "out", base_dir / "cache")) == 0
    baseline = _daily(base_dir / "out")

    _write_config(workdir / "config", {"demo": demo.clone_url(), "empty": empty.clone_url()})
    monkeypatch.chdir(workdir)
    out_a = workdir / "out_a"
    assert main(_argv(out_a, workdir / "cache_a")) == 0

    payload = _daily(out_a)
    assert set(payload["per_repo"]) == {"acme/demo", "acme/empty"}
    empty_block = payload["per_repo"]["acme/empty"]
    assert set(empty_block) == set(payload["totals"])
    assert empty_block["main_rework"] == ZERO_MAIN_REWORK
    assert empty_block["branch_churn"] == ZERO_BRANCH_CHURN
    assert payload["per_repo"]["acme/demo"] == baseline["per_repo"]["acme/demo"]
    assert payload["totals"] == baseline["totals"]


def test_commitless_repo_rerun_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem_statistics.__main__ import main

    demo, empty, workdir = _setup(tmp_path, monkeypatch)
    _write_config(workdir / "config", {"demo": demo.clone_url(), "empty": empty.clone_url()})
    monkeypatch.chdir(workdir)

    out_a, cache_a = workdir / "out_a", workdir / "cache_a"
    out_b, cache_b = workdir / "out_b", workdir / "cache_b"
    assert main(_argv(out_a, cache_a)) == 0
    snapshot = read_tree(out_a)
    assert snapshot  # something was written

    assert main(_argv(out_b, cache_b)) == 0
    assert read_tree(out_b) == snapshot

    # Reused cache: fetch_all runs on the cached bare clone of the empty origin.
    assert main(_argv(out_a, cache_a)) == 0
    assert read_tree(out_a) == snapshot
