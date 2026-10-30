"""#34 -- one failing repo must not take down the day's snapshot.

Offline: `file://` clones and a `MockTransport` in place of `github.make_client`.
Two unrelated failure causes prove the boundary is not tied to one call site:
  git:  `bad`'s clone URL does not exist -> CalledProcessError from gitrepo.ensure_repo
  http: `bad` clones fine, but its issues endpoint answers 500 -> HTTPStatusError
Expected RED reason: the exception propagating out of `main`, so no daily file is written.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import SyntheticRepo, read_tree

DAYS = ("2024-01-01", "2024-01-02")
WINDOW = "2024-01-01..2024-01-02"


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


def _client_factory(fail_issues_for: str | None):
    def make(token: str | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            assert path.endswith(("/issues", "/pulls")), path
            if fail_issues_for and path == f"/repos/acme/{fail_issues_for}/issues":
                return httpx.Response(500, json={"message": "boom"})
            return httpx.Response(200, json=[])

        return httpx.Client(transport=httpx.MockTransport(handler))

    return make


def _argv(out: Path, cache: Path) -> list[str]:
    return [
        "collect",
        "--since", DAYS[0],
        "--until", DAYS[1],
        "--out", str(out),
        "--cache-dir", str(cache),
    ]


def _daily(out: Path, day: str) -> dict:
    return json.loads((out / "daily" / f"{day}.json").read_text(encoding="utf-8"))


def _origin(tmp_path: Path, name: str) -> SyntheticRepo:
    repo = SyntheticRepo(tmp_path / f"{name}-origin")
    when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    repo.commit_file(f"{name}.py", f"{name}\n", f"init {name}", when=when)
    return repo


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str):
    """Returns (healthy_urls, all_urls, workdir); patches the HTTP client for `cause`."""
    import ecosystem_statistics.github as github

    a, b, bad = _origin(tmp_path, "demo-a"), _origin(tmp_path, "demo-b"), _origin(tmp_path, "bad")
    healthy = {"demo-a": a.clone_url(), "demo-b": b.clone_url()}
    bad_url = "file:///nonexistent/bad.git" if cause == "git" else bad.clone_url()
    monkeypatch.setattr(
        github, "make_client", _client_factory("bad" if cause == "http" else None)
    )
    return healthy, {**healthy, "bad": bad_url}, tmp_path / "workspace"


@pytest.mark.parametrize("cause", ["git", "http"])
def test_one_failing_repo_does_not_abort_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cause: str
) -> None:
    from ecosystem_statistics.__main__ import main

    healthy, everything, workdir = _setup(tmp_path, monkeypatch, cause)

    # Baseline: only the healthy repos.
    base_dir = tmp_path / "base"
    _write_config(base_dir / "config", healthy)
    monkeypatch.chdir(base_dir)
    assert main(_argv(base_dir / "out", base_dir / "cache")) == 0
    capsys.readouterr()

    _write_config(workdir / "config", everything)
    monkeypatch.chdir(workdir)
    out = workdir / "out"
    assert main(_argv(out, workdir / "cache")) == 0

    for day in DAYS:
        payload = _daily(out, day)
        baseline = _daily(base_dir / "out", day)
        assert payload["schema_version"] == 2
        assert payload["failed_repos"] == ["acme/bad"]
        assert set(payload["per_repo"]) == {"acme/demo-a", "acme/demo-b"}
        assert payload["per_repo"] == baseline["per_repo"]
        assert payload["totals"] == baseline["totals"]

    err = capsys.readouterr().err
    assert "acme/bad" in err
    assert WINDOW in err
    if cause == "git":
        assert "CalledProcessError" in err
        # git's own message (exc.stderr), not the command line, which holds the path too.
        assert "does not exist" in err or "Could not read from remote" in err or "fatal:" in err
    else:
        assert "HTTPStatusError" in err
        assert "500" in err


@pytest.mark.parametrize("cause", ["git", "http"])
def test_failing_repo_rerun_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    from ecosystem_statistics.__main__ import main

    _, everything, workdir = _setup(tmp_path, monkeypatch, cause)
    _write_config(workdir / "config", everything)
    monkeypatch.chdir(workdir)

    out_a, cache_a = workdir / "out_a", workdir / "cache_a"
    out_b, cache_b = workdir / "out_b", workdir / "cache_b"
    assert main(_argv(out_a, cache_a)) == 0
    snapshot = read_tree(out_a)
    assert snapshot

    marker = json.dumps([_daily(out_a, d)["failed_repos"] for d in DAYS])
    assert "acme/bad" in marker
    for forbidden in ("Error", "nonexistent", str(tmp_path)):
        assert forbidden not in marker

    assert main(_argv(out_b, cache_b)) == 0
    assert read_tree(out_b) == snapshot

    # Reused cache: fetch_all runs on the cached healthy clones.
    assert main(_argv(out_a, cache_a)) == 0
    assert read_tree(out_a) == snapshot


def test_all_repos_failing_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem_statistics.__main__ import main

    workdir = tmp_path / "workspace"
    _write_config(
        workdir / "config",
        {"x": "file:///nonexistent/x.git", "y": "file:///nonexistent/y.git"},
    )
    monkeypatch.chdir(workdir)
    out, cache = workdir / "out", workdir / "cache"

    from ecosystem_statistics.collect import NoRepoCollectedError

    with pytest.raises(NoRepoCollectedError):
        main(_argv(out, cache))
    daily_dir = out / "daily"
    assert not daily_dir.exists() or not list(daily_dir.glob("*.json"))
    assert not (out / "index.json").exists()

    proc = subprocess.run(
        [sys.executable, "-m", "ecosystem_statistics", *_argv(out, cache)],
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert not (out / "index.json").exists()


def test_token_never_reaches_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import base64

    from ecosystem_statistics.__main__ import main

    secret = "ghp_SuperSecretTokenValue123"
    monkeypatch.setenv("ECOSYSTEM_TOKEN", secret)
    healthy, everything, workdir = _setup(tmp_path, monkeypatch, "git")
    _write_config(workdir / "config", everything)
    monkeypatch.chdir(workdir)
    capsys.readouterr()
    assert main(_argv(workdir / "out", workdir / "cache")) == 0
    err = capsys.readouterr().err
    assert "acme/bad" in err and "CalledProcessError" in err
    basic = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
    for forbidden in (secret, basic, "extraheader", "AUTHORIZATION"):
        assert forbidden not in err
