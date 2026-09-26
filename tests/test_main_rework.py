"""R2 — main_rework young-line window boundary (plan #9).

For UTC day D, `main_rework = young_removed / added_to_main` over the backward-looking
window `W(D) = (end(D) - N days, end(D)]`. A line removed on day D is "young" only if the
commit that introduced it (found by following blame through renames) falls inside W(D).
This test pins a line's origin exactly 10 days before its removal, so N=21 must count it
as young and N=7 must not — the boundary the plan's window arithmetic exists to get right.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from conftest import SyntheticRepo


def test_ten_day_old_line_young_at_21_not_7(synthetic_repo: SyntheticRepo) -> None:
    repo = synthetic_repo
    day0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    day10 = datetime(2024, 1, 11, 12, 0, 0, tzinfo=timezone.utc)

    ten_lines = "\n".join(f"line{i}" for i in range(10)) + "\n"
    repo.commit_file("main.py", ten_lines, "day0: add 10 lines", when=day0)

    nine_lines = "\n".join(f"line{i}" for i in range(9)) + "\n"
    repo.commit_file("main.py", nine_lines, "day10: remove one line", when=day10)

    from ecosystem_statistics.churn import main_rework

    result_21 = main_rework(repo.path, date(2024, 1, 11), window_days=21)
    assert result_21.young_removed == 1
    assert result_21.added_to_main == 10
    assert result_21.value == 0.1

    result_7 = main_rework(repo.path, date(2024, 1, 11), window_days=7)
    assert result_7.young_removed == 0
    # day0's 10 added lines fall outside the 7-day window too, so the denominator is 0.
    assert result_7.added_to_main == 0
    assert result_7.value is None


def test_exclude_commits_drops_from_both_terms(synthetic_repo: SyntheticRepo) -> None:
    """`exclude_commits` (config/churn.yml) must drop a commit from both terms: as the
    top-level commit being examined for removed lines, and as the origin a removed line's
    blame points back to (plan #9)."""
    repo = synthetic_repo
    day0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    day10 = datetime(2024, 1, 11, 12, 0, 0, tzinfo=timezone.utc)

    ten_lines = "\n".join(f"line{i}" for i in range(10)) + "\n"
    day0_sha = repo.commit_file("main.py", ten_lines, "day0: add 10 lines", when=day0)

    nine_lines = "\n".join(f"line{i}" for i in range(9)) + "\n"
    day10_sha = repo.commit_file("main.py", nine_lines, "day10: remove one line", when=day10)

    from ecosystem_statistics.churn import main_rework

    baseline = main_rework(repo.path, date(2024, 1, 11), window_days=21)
    assert baseline.young_removed == 1
    assert baseline.added_to_main == 10

    # Excluding the *origin* commit: the removed line's blame lands on an excluded
    # commit, so it is no longer young; that same commit's own added lines also drop out
    # of added_to_main.
    excluded_origin = main_rework(
        repo.path, date(2024, 1, 11), window_days=21, exclude_commits=[day0_sha]
    )
    assert excluded_origin.young_removed == 0
    assert excluded_origin.added_to_main == 0

    # Excluding the *removal* commit itself: it is skipped entirely, so it contributes no
    # removed lines -- day0's added lines still count normally.
    excluded_removal = main_rework(
        repo.path, date(2024, 1, 11), window_days=21, exclude_commits=[day10_sha]
    )
    assert excluded_removal.young_removed == 0
    assert excluded_removal.added_to_main == 10


def test_negative_offset_evening_commit_buckets_into_next_utc_day(
    synthetic_repo: SyntheticRepo,
) -> None:
    """A commit timestamped 23:30 in a -02:00 local offset is 01:30 UTC the next
    calendar day -- day-bucketing must use the UTC date, never the offset's local date
    (AGENTS.md: "All days are UTC calendar days")."""
    repo = synthetic_repo
    base_day = datetime(2023, 12, 28, 12, 0, 0, tzinfo=timezone.utc)  # 5 days before, in-window
    ten_lines = "\n".join(f"line{i}" for i in range(10)) + "\n"
    repo.commit_file("main.py", ten_lines, "base: add 10 lines", when=base_day)

    # 2024-01-01T23:30:00-02:00 == 2024-01-02T01:30:00Z.
    local_evening = datetime(2024, 1, 1, 23, 30, 0, tzinfo=timezone(timedelta(hours=-2)))
    nine_lines = "\n".join(f"line{i}" for i in range(9)) + "\n"
    repo.commit_file("main.py", nine_lines, "remove one line", when=local_evening)

    from ecosystem_statistics.churn import main_rework

    result_local_date = main_rework(repo.path, date(2024, 1, 1), window_days=21)
    assert result_local_date.young_removed == 0

    result_utc_date = main_rework(repo.path, date(2024, 1, 2), window_days=21)
    assert result_utc_date.young_removed == 1


def test_no_ff_merge_commit_counts_toward_added_to_main(synthetic_repo: SyntheticRepo) -> None:
    """A `--no-ff` merge commit (`Merge branch '...'`) is itself a first-parent commit; the
    lines it brings in from the merged branch must count toward `added_to_main` just like a
    direct commit's lines would -- `added_to_main` sums *every* non-excluded first-parent
    commit in the window, merges included, not only direct commits (plan #9)."""
    repo = synthetic_repo
    seed_day = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    merge_day = datetime(2024, 2, 15, 12, 0, 0, tzinfo=timezone.utc)

    # Seeded well outside the 21-day window ending at merge_day, so its own added line
    # does not contaminate the count this test is isolating.
    repo.commit_file("main.py", "m\n", "seed: unrelated commit on main", when=seed_day)
    repo.branch("feature", start_point="main")

    five_lines = "\n".join(f"f{i}" for i in range(5)) + "\n"
    repo.commit_file("feature.py", five_lines, "feature: add 5 lines", when=seed_day)

    repo.checkout("main")
    repo.merge("feature", "Merge branch 'feature'", when=merge_day)

    from ecosystem_statistics.churn import main_rework

    result = main_rework(repo.path, date(2024, 2, 15), window_days=21)
    assert result.young_removed == 0
    assert result.added_to_main == 5
    assert result.value == 0.0


def test_exclude_paths_ignored_by_main_rework(synthetic_repo: SyntheticRepo) -> None:
    """`exclude_paths` (config/churn.yml's lockfile globs) must drop a path from both
    `main_rework` terms: an excluded path's added lines don't count toward
    `added_to_main`, and an excluded path's removed lines don't count as `young_removed`
    -- even though `main_rework` was never exercised with `exclude_paths` set before this
    test (only the CLI's end-to-end test touched it indirectly)."""
    repo = synthetic_repo
    day0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    day10 = datetime(2024, 1, 11, 12, 0, 0, tzinfo=timezone.utc)

    ten_lines = "\n".join(f"line{i}" for i in range(10)) + "\n"
    repo.commit_file("main.py", ten_lines, "day0: add 10 lines to main.py", when=day0)
    hundred_lock_lines = "\n".join(f"dep{i}" for i in range(100)) + "\n"
    repo.commit_file("package-lock.json", hundred_lock_lines, "day0: add lockfile", when=day0)

    nine_lines = "\n".join(f"line{i}" for i in range(9)) + "\n"
    repo.commit_file("main.py", nine_lines, "day10: remove 1 line from main.py", when=day10)
    fifty_lock_lines = "\n".join(f"dep{i}" for i in range(50)) + "\n"
    repo.commit_file(
        "package-lock.json", fifty_lock_lines, "day10: remove 50 lock lines", when=day10
    )

    from ecosystem_statistics.churn import main_rework

    result = main_rework(
        repo.path, date(2024, 1, 11), window_days=21, exclude_paths=["package-lock.json"]
    )
    # Only main.py's 1 removed / 10 added lines count; the lockfile's 100 added / 50
    # removed lines must be excluded from both terms.
    assert result.young_removed == 1
    assert result.added_to_main == 10
    assert result.value == 0.1
