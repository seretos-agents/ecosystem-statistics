"""R1-R4 -- `publish()` writes deterministic output to a `data` branch on a git remote,
committing only on a real diff, and the `publish` CLI subcommand computes its default
date window and validates `--since`/`--until` (plan #10).

All git-remote behaviour is proven against a local bare repo (`bare_origin` fixture in
conftest.py) -- never the real GitHub origin (AGENTS.md: tests never touch the network).
`publish()`'s `collect` parameter is the test seam named in the plan: production wires it
to a lambda around `collect.run_collect`, these tests wire it to a fake that calls the
real `output.write_daily`/`output.rebuild_index` writers directly, so the committed bytes
are in the real on-disk format without needing a GitHub mock for the whole collector.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Callable

import pytest

from ecosystem_statistics import output


def _init_repo_dir(path: Path) -> Path:
    """A fresh, otherwise-empty git repository standing in for the "code checkout"
    `publish()` runs its git commands from (in production: the actions/checkout'd
    working copy on `main`). `publish()` never reads its content -- it only needs a
    valid repository from which to `fetch`/`worktree add` against `remote`."""
    subprocess.run(
        ["git", "init", "--quiet", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _payload_for(day: str) -> dict:
    return {"schema_version": 1, "date": day, "totals": {}, "per_repo": {}}


def _fake_collect(payloads: dict[str, dict]) -> Callable[[Path], None]:
    """A `collect` callable that writes the given day->payload map via the real
    output writers -- exercising the real on-disk format without a GitHub mock."""

    def collect(out_dir: Path) -> None:
        for day_str, payload in payloads.items():
            output.write_daily(out_dir, day_str, payload)
        output.rebuild_index(out_dir)

    return collect


def _worktree_count(repo_dir: Path) -> int:
    listing = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return listing.count("worktree ")


# -- R1 ----------------------------------------------------------------------


def test_publish_creates_orphan_data_branch(tmp_path: Path, bare_origin: Path) -> None:
    from ecosystem_statistics import gitrepo, publish

    repo_dir = _init_repo_dir(tmp_path / "checkout")
    worktree_dir = tmp_path / "data-worktree"

    result = publish.publish(
        repo_dir,
        remote=str(bare_origin),
        branch="data",
        worktree_dir=worktree_dir,
        message="data: collect 2024-01-01..2024-01-01",
        collect=_fake_collect({}),  # empty collect -> no diff, no content commit
    )

    ls_remote = gitrepo.run(bare_origin, "ls-remote", "--heads", str(bare_origin), "data")
    assert "refs/heads/data" in ls_remote

    tip = gitrepo.run(bare_origin, "rev-parse", "data").strip()
    root = gitrepo.run(bare_origin, "rev-list", "--max-parents=0", "data").strip()
    assert root == tip

    tree = gitrepo.run(bare_origin, "show", "-s", "--format=%T", tip).strip()
    assert tree == gitrepo.EMPTY_TREE_SHA

    merge_base = subprocess.run(
        ["git", "-C", str(bare_origin), "merge-base", "main", "data"],
        capture_output=True,
        text=True,
    )
    assert merge_base.returncode != 0

    assert result.created_branch is True
    assert result.committed is False
    assert result.pushed is False


# -- R2 ------------------------------------------------------------------------


def test_publish_three_days_is_one_commit(tmp_path: Path, bare_origin: Path) -> None:
    from ecosystem_statistics import gitrepo, publish

    repo_dir = _init_repo_dir(tmp_path / "checkout")
    worktree_dir = tmp_path / "data-worktree"

    days = ["2024-01-01", "2024-01-02", "2024-01-03"]
    payloads = {d: _payload_for(d) for d in days}

    result = publish.publish(
        repo_dir,
        remote=str(bare_origin),
        branch="data",
        worktree_dir=worktree_dir,
        message="data: collect 2024-01-01..2024-01-03",
        collect=_fake_collect(payloads),
    )

    tip = gitrepo.run(bare_origin, "rev-parse", "data").strip()
    root = gitrepo.run(bare_origin, "rev-list", "--max-parents=0", "data").strip()
    assert root != tip
    assert gitrepo.run(bare_origin, "rev-list", "--count", f"{root}..{tip}").strip() == "1"

    name_status = gitrepo.run(bare_origin, "diff", "--name-status", root, tip).strip().splitlines()
    assert sorted(name_status) == sorted(
        [
            "A\tdaily/2024-01-01.json",
            "A\tdaily/2024-01-02.json",
            "A\tdaily/2024-01-03.json",
            "A\tindex.json",
        ]
    )

    for day in days:
        committed_bytes = gitrepo.run_bytes(bare_origin, "show", f"{tip}:daily/{day}.json")
        assert committed_bytes == output.dump_json_bytes(payloads[day])

    assert result.created_branch is True
    assert result.committed is True
    assert result.pushed is True

    # Edge coverage: the worktree is removed afterwards -- the code checkout shows
    # only its own (main) worktree, and the target directory no longer exists.
    assert _worktree_count(repo_dir) == 1
    assert not worktree_dir.exists()


def test_publish_updates_existing_data_branch(tmp_path: Path, bare_origin: Path) -> None:
    from ecosystem_statistics import gitrepo, publish

    repo_dir = _init_repo_dir(tmp_path / "checkout")
    worktree_dir = tmp_path / "data-worktree"

    seed_day = "2023-12-31"
    seed_result = publish.publish(
        repo_dir,
        remote=str(bare_origin),
        branch="data",
        worktree_dir=worktree_dir,
        message=f"data: collect {seed_day}..{seed_day}",
        collect=_fake_collect({seed_day: _payload_for(seed_day)}),
    )
    assert seed_result.created_branch is True
    assert seed_result.committed is True
    old_tip = gitrepo.run(bare_origin, "rev-parse", "data").strip()

    days = ["2024-01-01", "2024-01-02", "2024-01-03"]
    payloads = {d: _payload_for(d) for d in days}

    result = publish.publish(
        repo_dir,
        remote=str(bare_origin),
        branch="data",
        worktree_dir=worktree_dir,
        message="data: collect 2024-01-01..2024-01-03",
        collect=_fake_collect(payloads),
    )

    assert result.created_branch is False
    assert result.committed is True
    assert result.pushed is True

    new_tip = gitrepo.run(bare_origin, "rev-parse", "data").strip()
    assert gitrepo.run(bare_origin, "rev-parse", f"{new_tip}^").strip() == old_tip

    name_status = gitrepo.run(
        bare_origin, "diff", "--name-status", old_tip, new_tip
    ).strip().splitlines()
    assert sorted(name_status) == sorted(
        [
            "A\tdaily/2024-01-01.json",
            "A\tdaily/2024-01-02.json",
            "A\tdaily/2024-01-03.json",
            "M\tindex.json",
        ]
    )

    index = json.loads(gitrepo.run_bytes(bare_origin, "show", f"{new_tip}:index.json"))
    assert index["days"] == [seed_day, *days]


def test_publish_cleans_worktree_on_error(tmp_path: Path, bare_origin: Path) -> None:
    """Edge coverage for R2: the worktree is removed even when `collect` raises."""
    from ecosystem_statistics import publish

    repo_dir = _init_repo_dir(tmp_path / "checkout")
    worktree_dir = tmp_path / "data-worktree"

    class Boom(RuntimeError):
        pass

    def failing_collect(out_dir: Path) -> None:
        raise Boom("collect blew up")

    with pytest.raises(Boom):
        publish.publish(
            repo_dir,
            remote=str(bare_origin),
            branch="data",
            worktree_dir=worktree_dir,
            message="data: collect 2024-01-01..2024-01-01",
            collect=failing_collect,
        )

    assert not worktree_dir.exists()
    assert _worktree_count(repo_dir) == 1


# -- R3 ------------------------------------------------------------------------


def test_republish_same_window_is_noop(
    tmp_path: Path, bare_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem_statistics import gitrepo, publish

    repo_dir = _init_repo_dir(tmp_path / "checkout")
    worktree_dir = tmp_path / "data-worktree"

    day = "2024-02-01"
    payloads = {day: _payload_for(day)}

    first = publish.publish(
        repo_dir,
        remote=str(bare_origin),
        branch="data",
        worktree_dir=worktree_dir,
        message=f"data: collect {day}..{day}",
        collect=_fake_collect(payloads),
    )
    assert first.committed is True
    tip_before = gitrepo.run(bare_origin, "rev-parse", "data").strip()

    recorded_calls: list[tuple[str, ...]] = []
    original_git = publish._git

    def recording_git(cwd: Path, *args: str, **kwargs: object):
        recorded_calls.append(args)
        return original_git(cwd, *args, **kwargs)

    monkeypatch.setattr(publish, "_git", recording_git)

    result = publish.publish(
        repo_dir,
        remote=str(bare_origin),
        branch="data",
        worktree_dir=worktree_dir,
        message=f"data: collect {day}..{day}",
        collect=_fake_collect(payloads),
    )

    tip_after = gitrepo.run(bare_origin, "rev-parse", "data").strip()
    assert tip_after == tip_before
    assert result == publish.PublishResult(created_branch=False, committed=False, pushed=False)
    assert not any(
        "push" in arg or "commit" in arg for call in recorded_calls for arg in call
    )


# -- R4 ------------------------------------------------------------------------


def test_cli_publish_default_window_is_yesterday_utc(
    tmp_path: Path, bare_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem_statistics import gitrepo, publish
    from ecosystem_statistics.__main__ import main

    monkeypatch.setattr(publish, "_utc_today", lambda: date(2024, 3, 1))

    recorded: dict[str, date] = {}

    def fake_run_collect(*, since: date, until: date, out_dir: Path, cache_dir: Path, config_dir: Path) -> None:
        recorded["since"] = since
        recorded["until"] = until
        # Force a real diff so the CLI's constructed commit message is observable on
        # `data` afterwards, not just inside this recorder's closure.
        output.write_json(out_dir / "marker.json", {"since": str(since), "until": str(until)})

    monkeypatch.setattr(publish, "run_collect", fake_run_collect)

    repo_dir = _init_repo_dir(tmp_path / "checkout")
    monkeypatch.chdir(repo_dir)
    worktree_dir = tmp_path / "data-worktree"

    assert (
        main(["publish", "--remote", str(bare_origin), "--worktree", str(worktree_dir)]) == 0
    )

    assert recorded["since"] == date(2024, 2, 29)
    assert recorded["until"] == date(2024, 2, 29)

    message = gitrepo.run(bare_origin, "log", "-1", "--format=%s", "data").strip()
    assert message == "data: collect 2024-02-29..2024-02-29"


def test_cli_publish_explicit_window(
    tmp_path: Path, bare_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem_statistics import publish
    from ecosystem_statistics.__main__ import main

    recorded: dict[str, date] = {}

    def fake_run_collect(*, since: date, until: date, out_dir: Path, cache_dir: Path, config_dir: Path) -> None:
        recorded["since"] = since
        recorded["until"] = until

    monkeypatch.setattr(publish, "run_collect", fake_run_collect)

    repo_dir = _init_repo_dir(tmp_path / "checkout")
    monkeypatch.chdir(repo_dir)
    worktree_dir = tmp_path / "data-worktree"

    assert (
        main(
            [
                "publish",
                "--since", "2024-05-01",
                "--until", "2024-05-03",
                "--remote", str(bare_origin),
                "--worktree", str(worktree_dir),
            ]
        )
        == 0
    )

    assert recorded["since"] == date(2024, 5, 1)
    assert recorded["until"] == date(2024, 5, 3)


def test_cli_publish_rejects_half_window(capsys: pytest.CaptureFixture[str]) -> None:
    """`SystemExit(2)` alone is not distinguishing: argparse already exits 2 today
    because `publish` isn't a recognised subcommand at all, which would make this
    assertion pass before the CLI (or the since/until pairing check) exists. Pin the
    stderr text too, so the RED failure is attributable to the missing pairing
    validation specifically, not to the subcommand being absent."""
    from ecosystem_statistics.__main__ import main

    with pytest.raises(SystemExit) as since_only:
        main(["publish", "--since", "2024-05-01", "--worktree", "wt"])
    assert since_only.value.code == 2
    stderr = capsys.readouterr().err
    assert "invalid choice" not in stderr
    assert "--since" in stderr and "--until" in stderr

    with pytest.raises(SystemExit) as until_only:
        main(["publish", "--until", "2024-05-01", "--worktree", "wt"])
    assert until_only.value.code == 2
    stderr = capsys.readouterr().err
    assert "invalid choice" not in stderr
    assert "--since" in stderr and "--until" in stderr
