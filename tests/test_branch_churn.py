"""R1 — branch_churn per merged PR (plan #9).

`branch_churn = 1 - net_added/gross_added`: gross_added sums the "added" numstat column
over every non-merge commit between the merge's two parents (`M^1..M^2`), net_added sums
it over the single `M^1..M^2` diff. A PR with fixup commits (added, then rewritten) should
show more gross churn than net — critique that reshaped code before it ever reached main.
"""

from __future__ import annotations

from conftest import SyntheticRepo, make_fixup_branch


def test_fixup_commits_yield_expected_churn(synthetic_repo: SyntheticRepo) -> None:
    repo = synthetic_repo
    repo.commit_file("README.md", "base\n", "initial commit on main")
    repo.branch("feature")

    make_fixup_branch(repo)

    # A commit on main after the fork, later merged back into the feature branch before
    # the final PR merge — must not inflate gross (excluded by the M^1..M^2 range, since
    # it's already an ancestor of M^1) or net (identical content in both trees at the end).
    repo.checkout("main")
    repo.commit_file("OTHER.md", "unrelated main note\n", "unrelated main commit")
    repo.checkout("feature")
    repo.merge("main", "merge main into feature")

    repo.checkout("main")
    merge_sha = repo.merge("feature", "Merge pull request #1 from feature")

    from ecosystem_statistics.churn import branch_churn

    result = branch_churn(repo.path, merge_sha, exclude_paths=["package-lock.json"])

    assert result.gross_added == 16
    assert result.net_added == 12
    assert result.value == 0.25


def test_differently_shaped_pr_yields_independently_computed_churn(
    synthetic_repo: SyntheticRepo,
) -> None:
    """A second PR with a different commit shape and different expected numbers than
    `test_fixup_commits_yield_expected_churn` above -- a hardcoded/stub branch_churn that
    ignores its inputs and always returns 16/12/0.25 would pass that test but fail this
    one (test-critic F1)."""
    repo = synthetic_repo
    repo.commit_file("README.md", "base\n", "initial commit on main")
    repo.branch("feature")

    eight_lines = "\n".join(f"g{i}" for i in range(8)) + "\n"
    repo.commit_file("gadget.py", eight_lines, "c1: add 8 lines")

    rewritten = "\n".join(
        [f"g{i}-rewritten" for i in range(3)] + [f"g{i}" for i in range(3, 8)]
    ) + "\n"
    repo.commit_file("gadget.py", rewritten, "c2: rewrite 3 lines")

    repo.checkout("main")
    merge_sha = repo.merge("feature", "Merge pull request #2 from feature")

    from ecosystem_statistics.churn import branch_churn

    result = branch_churn(repo.path, merge_sha, exclude_paths=["package-lock.json"])

    # gross: c1 adds 8, c2 replaces 3 (3 added) = 11. net: gadget.py is new to main, so
    # its final 8 lines are all "added" in the M^1..M^2 diff. value = 1 - 8/11 = 0.2727.
    assert result.gross_added == 11
    assert result.net_added == 8
    assert result.value == 0.2727


def test_gross_zero_yields_null_value(synthetic_repo: SyntheticRepo) -> None:
    """A merge whose branch contributed no non-merge commits between its two parents
    (e.g. a fork that only ever merged main back in, never adding its own content) has
    gross_added == 0 -- `value` must be null, not a ZeroDivisionError or a bogus 0/0."""
    repo = synthetic_repo
    repo.commit_file("README.md", "base\n", "initial commit on main")
    repo.branch("feature")

    repo.checkout("main")
    repo.commit_file("OTHER.md", "main moves on\n", "main moves on")

    repo.checkout("feature")
    repo.merge("main", "merge main into feature")

    repo.checkout("main")
    merge_sha = repo.merge("feature", "Merge pull request #3 from feature")

    from ecosystem_statistics.churn import branch_churn

    result = branch_churn(repo.path, merge_sha, exclude_paths=["package-lock.json"])

    assert result.gross_added == 0
    assert result.net_added == 0
    assert result.value is None
