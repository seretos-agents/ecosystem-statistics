"""R6 — pure renames must not count as churn or rework (plan #9, F1).

Every diff/numstat the collector reads must pass `-M50%` (rename detection on), not
`--no-renames`. A commit that only `git mv`s a file should move neither metric: numstat
reports 0 added/0 deleted for a pure rename, and `git blame` keeps following the file's
history across the rename so a moved line's origin date does not change. Without rename
detection, a `git mv` of a 100-line file would show as a 100-line delete plus a 100-line
add — inflating branch_churn's gross/net and misdating main_rework's blame origins to the
day of the move instead of the day the content actually landed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from conftest import SyntheticRepo, make_fixup_branch


def test_branch_rename_is_not_churn(synthetic_repo: SyntheticRepo) -> None:
    repo = synthetic_repo
    util_lines = "\n".join(f"u{i}" for i in range(100)) + "\n"
    repo.commit_file("util.py", util_lines, "add util.py on main")
    repo.branch("feature")

    make_fixup_branch(repo)
    repo.mv("util.py", "helpers.py")
    repo.commit("rename util.py to helpers.py")

    repo.checkout("main")
    merge_sha = repo.merge("feature", "Merge pull request #1 from feature")

    from ecosystem_statistics.churn import branch_churn

    result = branch_churn(repo.path, merge_sha, exclude_paths=["package-lock.json"])

    # Same numbers as the plain fixup-commit case (test_branch_churn.py) — the 100-line
    # pure rename must contribute 0 to both gross_added and net_added.
    assert result.gross_added == 16
    assert result.net_added == 12
    assert result.value == 0.25


def test_main_rename_is_not_rework(synthetic_repo: SyntheticRepo) -> None:
    repo = synthetic_repo
    day0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    day5 = datetime(2024, 1, 6, 12, 0, 0, tzinfo=timezone.utc)
    day10 = datetime(2024, 1, 11, 12, 0, 0, tzinfo=timezone.utc)

    ten_lines = "\n".join(f"a{i}" for i in range(10)) + "\n"
    repo.commit_file("a.py", ten_lines, "day0: add a.py", when=day0)

    repo.mv("a.py", "b.py")
    repo.commit("day5: rename a.py to b.py", when=day5)

    nine_lines = "\n".join(f"a{i}" for i in range(9)) + "\n"
    repo.commit_file("b.py", nine_lines, "day10: remove one line", when=day10)

    from ecosystem_statistics.churn import main_rework

    # The rename commit itself is a pure rename: 0 young_removed, and its own numstat
    # contributes 0 added lines, so added_to_main here is entirely day0's 10 lines.
    result_day5 = main_rework(repo.path, date(2024, 1, 6), window_days=21)
    assert result_day5.young_removed == 0
    assert result_day5.added_to_main == 10

    # The removed line's blame origin is day0 (Jan 1), not the day5 rename — so at N=21
    # (window opens Dec 22) it is young, but at N=7 (window opens Jan 4) it is not.
    result_day10_21 = main_rework(repo.path, date(2024, 1, 11), window_days=21)
    assert result_day10_21.young_removed == 1

    result_day10_7 = main_rework(repo.path, date(2024, 1, 11), window_days=7)
    assert result_day10_7.young_removed == 0


def test_rename_with_content_edit_adds_only_edited_lines_to_gross(
    synthetic_repo: SyntheticRepo,
) -> None:
    """A commit that renames a file AND edits some of its lines must add only the edited
    line count to `gross_added` -- not the whole (renamed) file's line count, which a
    rename-blind numstat would report as a full delete-of-old plus add-of-new."""
    repo = synthetic_repo
    hundred_lines = "\n".join(f"u{i}" for i in range(100)) + "\n"
    repo.commit_file("util.py", hundred_lines, "add util.py on main")
    repo.branch("feature")

    repo.mv("util.py", "helpers.py")
    edited = (
        "\n".join([f"u{i}-edited" for i in range(3)] + [f"u{i}" for i in range(3, 100)]) + "\n"
    )
    repo.write("helpers.py", edited)
    repo.commit("rename util.py to helpers.py and edit 3 lines")

    repo.checkout("main")
    merge_sha = repo.merge("feature", "Merge pull request #1 from feature")

    from ecosystem_statistics.churn import branch_churn

    result = branch_churn(repo.path, merge_sha)

    # 3 lines changed out of 100 -- a rename-blind implementation would instead see a
    # 100-line delete plus a 100-line add.
    assert result.gross_added == 3
    assert result.net_added == 3
    assert result.value == 0.0


def test_rename_into_excluded_path_is_excluded(synthetic_repo: SyntheticRepo) -> None:
    """A rename whose *destination* path falls under `exclude_paths` must be excluded from
    both `gross_added` and `net_added` -- matching only the pure-rename's old path is not
    enough, since a rename combined with a content edit would otherwise leak the edited
    lines into the counts."""
    repo = synthetic_repo
    hundred_lines = "\n".join(f"u{i}" for i in range(100)) + "\n"
    repo.commit_file("util.py", hundred_lines, "add util.py on main")
    repo.branch("feature")

    ten_lines = "\n".join(f"line{i}" for i in range(10)) + "\n"
    repo.commit_file("feature.py", ten_lines, "c1: add 10 lines")
    rewritten = (
        "\n".join([f"line{i}-rewritten" for i in range(4)] + [f"line{i}" for i in range(4, 10)])
        + "\n"
    )
    repo.commit_file("feature.py", rewritten, "c2: rewrite 4 lines")
    final = rewritten.rstrip("\n") + "\nline10\nline11\n"
    repo.commit_file("feature.py", final, "c3: add 2 lines")

    # Rename util.py into an excluded destination AND edit 5 of its lines in the same
    # commit -- without excluding by the *new* path, this edit's 5 added/5 removed lines
    # would leak into gross/net.
    repo.mv("util.py", "package-lock.json")
    edited_lock = (
        "\n".join([f"u{i}-edited" for i in range(5)] + [f"u{i}" for i in range(5, 100)]) + "\n"
    )
    repo.write("package-lock.json", edited_lock)
    repo.commit("rename util.py into an excluded path and edit 5 lines")

    repo.checkout("main")
    merge_sha = repo.merge("feature", "Merge pull request #1 from feature")

    from ecosystem_statistics.churn import branch_churn

    result = branch_churn(repo.path, merge_sha, exclude_paths=["package-lock.json"])

    # Same numbers as a plain fixup case with no lockfile at all (test_branch_churn.py) --
    # the renamed-and-edited excluded file must contribute 0 to both gross and net.
    assert result.gross_added == 16
    assert result.net_added == 12
    assert result.value == 0.25
