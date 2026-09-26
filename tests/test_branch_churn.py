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
