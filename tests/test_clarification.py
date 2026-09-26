"""R2 (plan #17) -- pure parser and rule units for `ecosystem_statistics.clarification`.

This module has no GitHub I/O and no wall clock: `daily_breakdown(issues, days)` turns a
repo's `escalations.IssueHistory` list into a per-UTC-day breakdown of how often the
gatekeeper released a package without asking a human, and how heavy the clarifications,
frames, lane splits, re-cuts and falsified premises were.

Importing a module that does not exist yet is the deliberate RED for every test in this
file (plan #17 R2: "RED is ModuleNotFoundError") -- `clarification.py` is pure, brand-new,
and not created until the implement phase. `import ecosystem_statistics.clarification as
clarification` (rather than `from ecosystem_statistics import clarification`) is
deliberate: the latter's absent-submodule case raises `ImportError: cannot import name
...`, not literally `ModuleNotFoundError` -- see test_escalations.py's identical note for
plan #11.
"""

from __future__ import annotations

from datetime import date

import ecosystem_statistics.clarification as clarification
from conftest import clarification_comment, escaped, fenced, frame_block, released_comment
from ecosystem_statistics.escalations import IssueHistory

ZERO_BREAKDOWN = {
    "released": 0,
    "released_without_asking": 0,
    "clarifications": 0,
    "questions": 0,
    "hard_clarifications": 0,
    "frames": 0,
    "frames_ac_rewritten": 0,
    "premises": 0,
    "not_proven": 0,
    "frames_without_block": 0,
    "lane_splits": 0,
    "re_cuts": 0,
    "premise_falsified": 0,
    "value": None,
}


def _ts(day: date, hour: int = 9, minute: int = 0) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z"


# ---------------------------------------------------------------------------
# Clarification / questions / "unusually hard" threshold
# ---------------------------------------------------------------------------


def test_two_and_three_questions_are_not_hard_but_four_is() -> None:
    day = date(2024, 3, 5)
    issues = [
        IssueHistory(number=1, closed_at=None, comments=((_ts(day), clarification_comment(2)),)),
        IssueHistory(number=2, closed_at=None, comments=((_ts(day), clarification_comment(3)),)),
        IssueHistory(number=3, closed_at=None, comments=((_ts(day), clarification_comment(4)),)),
    ]

    breakdown = clarification.daily_breakdown(issues, [day])

    assert breakdown[day]["clarifications"] == 3
    assert breakdown[day]["questions"] == 2 + 3 + 4
    assert breakdown[day]["hard_clarifications"] == 1  # only the 4-question comment


def test_question_marker_outside_a_clarification_comment_is_not_counted() -> None:
    day = date(2024, 3, 5)
    body = "Just a status note.\n\n### Q1\nWhat about this?\n"
    issue = IssueHistory(number=1, closed_at=None, comments=((_ts(day), body),))

    breakdown = clarification.daily_breakdown([issue], [day])

    assert breakdown[day]["clarifications"] == 0
    assert breakdown[day]["questions"] == 0
    assert breakdown[day]["hard_clarifications"] == 0


# ---------------------------------------------------------------------------
# Frame block: raw / fenced / HTML-escaped, and #17's own verbatim example
# ---------------------------------------------------------------------------


def test_frame_block_parsed_raw_fenced_and_escaped() -> None:
    heading = "## Frame (gatekeeper)\n\n"
    day = date(2024, 3, 5)
    raw_body = heading + frame_block(ac_rewritten="yes", premises=2, not_proven=0)
    fenced_body = heading + fenced(frame_block(ac_rewritten="yes", premises=2, not_proven=0))
    escaped_body = heading + escaped(frame_block(ac_rewritten="yes", premises=2, not_proven=0))
    # test-critic tautology::F1: every other block in this batch has ac_rewritten:
    # "yes", so an implementation that increments frames_ac_rewritten for every block
    # found -- never actually reading the field -- would still pass. This fourth
    # issue's block has ac_rewritten: "no", so frames_ac_rewritten must stay at 3
    # (not rise to 4) while frames and premises both still count it.
    not_rewritten_body = heading + frame_block(ac_rewritten="no", premises=1, not_proven=0)
    issues = [
        IssueHistory(number=1, closed_at=None, comments=((_ts(day), raw_body),)),
        IssueHistory(number=2, closed_at=None, comments=((_ts(day), fenced_body),)),
        IssueHistory(number=3, closed_at=None, comments=((_ts(day), escaped_body),)),
        IssueHistory(number=4, closed_at=None, comments=((_ts(day), not_rewritten_body),)),
    ]

    breakdown = clarification.daily_breakdown(issues, [day])

    assert breakdown[day]["frames"] == 4
    assert breakdown[day]["frames_ac_rewritten"] == 3
    assert breakdown[day]["premises"] == 7
    assert breakdown[day]["frames_without_block"] == 0


def test_verbatim_frame_from_spec_md_gives_frames_1_ac_1_premises_3_not_proven_0() -> None:
    """#17's own Frame comment (`.adev/17-1/spec.md`, comment 5847722532), verbatim --
    including the backticked, mid-sentence mentions of `## Clarification needed
    (gatekeeper)`, `### Q<n>`, `## Frame (gatekeeper)` etc. in its "Premises to verify"
    paragraph, which must NOT themselves be read as headings or markers."""
    day = date(2024, 3, 4)
    body = (
        "## Frame (gatekeeper)\n\n"
        "Symptom: the owner cannot see, per UTC day, how often the gatekeeper releases "
        "a ticket on its own vs. how often it has to ask a human, or how heavy those "
        "clarifications were\n"
        "Acceptance criterion: running the collect CLI over a fixed window of fixture "
        "issue comments writes a `clarification` field into daily/YYYY-MM-DD.json. The "
        "field's counts match the expected values for: packages released by the "
        "gatekeeper without asking; `## Clarification needed (gatekeeper)` comments and "
        "the `### Q<n>` questions in them, with a comment holding 2 questions not "
        "flagged and a comment holding 4 questions counted in the separate \"unusually "
        "hard\" counter; frames whose acceptance criterion was rewritten; premises and "
        "`Not proven by this package:` clauses per frame; `## Lane split` and "
        "`## Re-cut` comments; and `PREMISE FALSIFIED:` lines. Each comment is counted "
        "on the UTC day of its timestamp. A comment with none of these headings adds 0, "
        "and two runs over the same window produce byte-identical files.\n"
        "Premises to verify before planning: the heading and question shapes "
        "(`## Clarification needed (gatekeeper)`, `### Q<n>`, `## Frame (gatekeeper)`, "
        "`## Lane split (gatekeeper)`, `## Re-cut (gatekeeper)`, `PREMISE FALSIFIED:`) "
        "come from the ticket text and the closed #2. Nobody checked them against the "
        "agent-ticket-orchestrator gatekeeper skill, which is outside local_path; frames "
        "posted since 2026-09-26 carry a `<!-- gatekeeper:frame v1 -->` block "
        "(ac_rewritten, premises, not_proven), seen on #9 and #11. Earlier frames may "
        "only have prose, and nothing here checked whether the block is emitted on "
        "every frame; \"4+ questions counts as unusually hard to clarify\" is "
        "attributed to \"the spec\" in #15/#8. The spec itself was not found under "
        "local_path\n\n"
        "The ticket's own finish line measured an internal quantity; the package is "
        "built and reviewed against the symptom above.\n\n"
        "Object by replying on this ticket.\n\n"
        "<!-- gatekeeper:frame v1\n"
        "ac_rewritten: yes\n"
        "premises: 3\n"
        "not_proven: 0\n"
        "-->\n"
    )
    issue = IssueHistory(number=17, closed_at=None, comments=((_ts(day), body),))

    breakdown = clarification.daily_breakdown([issue], [day])

    assert breakdown[day]["frames"] == 1
    assert breakdown[day]["frames_ac_rewritten"] == 1
    assert breakdown[day]["premises"] == 3
    assert breakdown[day]["not_proven"] == 0
    assert breakdown[day]["frames_without_block"] == 0


# ---------------------------------------------------------------------------
# PREMISE FALSIFIED marker: bulleted / bold / blockquoted count; mid-sentence and
# backticked do not
# ---------------------------------------------------------------------------


def test_premise_falsified_counts_bulleted_bold_blockquoted_not_midsentence_or_backticked() -> None:
    day = date(2024, 3, 5)
    body = (
        "- PREMISE FALSIFIED: reason one\n"
        "**PREMISE FALSIFIED:** reason two\n"
        "> PREMISE FALSIFIED: reason three\n"
        "We discussed PREMISE FALSIFIED: not really, mid-sentence.\n"
        "`PREMISE FALSIFIED:` a backticked mention.\n"
    )
    issue = IssueHistory(number=1, closed_at=None, comments=((_ts(day), body),))

    breakdown = clarification.daily_breakdown([issue], [day])

    assert breakdown[day]["premise_falsified"] == 3


# ---------------------------------------------------------------------------
# Heading gate: the "(gatekeeper)" suffix is required; a prefix collision does not count
# ---------------------------------------------------------------------------


def test_heading_without_gatekeeper_suffix_and_framework_prefix_collision_do_not_count() -> None:
    day = date(2024, 3, 5)
    body = (
        "## Released in v1.2\n\n"
        "Shipped, but not via the gatekeeper heading.\n\n"
        "## Framework (gatekeeper)\n\n"
        "Just a section about the framework, not a Frame heading.\n"
    )
    issue = IssueHistory(number=1, closed_at=None, comments=((_ts(day), body),))

    breakdown = clarification.daily_breakdown([issue], [day])

    assert breakdown[day]["released"] == 0
    assert breakdown[day]["frames"] == 0
    assert breakdown[day]["frames_without_block"] == 0


# ---------------------------------------------------------------------------
# Released-without-asking: package grouping via the Released comment's Package: line
# ---------------------------------------------------------------------------


def test_clarification_on_child_counts_as_asked_via_released_children_list() -> None:
    d1 = date(2024, 3, 4)
    d2 = date(2024, 3, 5)
    issue_child = IssueHistory(
        number=8, closed_at=None, comments=((_ts(d1), clarification_comment(1)),)
    )
    issue_epic = IssueHistory(
        number=9,
        closed_at=None,
        comments=((_ts(d2), released_comment("epic #9 (children #8, #10)")),),
    )

    breakdown = clarification.daily_breakdown([issue_child, issue_epic], [d1, d2])

    assert breakdown[d2]["released"] == 1
    assert breakdown[d2]["released_without_asking"] == 0


def test_released_with_no_package_line_falls_back_to_own_ticket() -> None:
    """test-critic tautology::F3: ticket #42 has a prior Clarification before its
    no-`Package:`-line Released, so whether the fallback key/member is really `T`
    itself is observable -- an implementation that treats a missing `Package:` line as
    having no members (always "without asking"), or that files it under some other
    key, would wrongly still show `released_without_asking == 1` instead of 0."""
    day = date(2024, 3, 5)
    issue = IssueHistory(
        number=42,
        closed_at=None,
        comments=(
            (_ts(day, 9, 0), clarification_comment(1)),
            (_ts(day, 10, 0), released_comment(None)),
        ),
    )

    breakdown = clarification.daily_breakdown([issue], [day])

    assert breakdown[day]["released"] == 1
    assert breakdown[day]["released_without_asking"] == 0
    assert breakdown[day]["value"] == 0.0


def test_combined_clarification_and_released_comment_counts_as_asked() -> None:
    """When one comment carries both headings, the clarification is processed first
    (plan #17), so the same comment's Released sees its own ticket as already asked."""
    day = date(2024, 3, 5)
    body = clarification_comment(1) + "\n" + released_comment("single #1")
    issue = IssueHistory(number=1, closed_at=None, comments=((_ts(day), body),))

    breakdown = clarification.daily_breakdown([issue], [day])

    assert breakdown[day]["clarifications"] == 1
    assert breakdown[day]["released"] == 1
    assert breakdown[day]["released_without_asking"] == 0


# ---------------------------------------------------------------------------
# Empty day
# ---------------------------------------------------------------------------


def test_empty_day_gives_all_zeros_and_null_value() -> None:
    day = date(2024, 3, 5)

    breakdown = clarification.daily_breakdown([], [day])

    assert breakdown[day] == ZERO_BREAKDOWN
