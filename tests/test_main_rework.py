"""R2 — main_rework young-line window boundary (plan #9).

For UTC day D, `main_rework = young_removed / added_to_main` over the backward-looking
window `W(D) = (end(D) - N days, end(D)]`. A line removed on day D is "young" only if the
commit that introduced it (found by following blame through renames) falls inside W(D).
This test pins a line's origin exactly 10 days before its removal, so N=21 must count it
as young and N=7 must not — the boundary the plan's window arithmetic exists to get right.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

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
