"""R2 (plan #13) -- pure parser and dedupe units for `ecosystem_statistics.regression_chains`.

This module has no GitHub I/O and no wall clock: `chain_members` turns one comment body
into the set of tickets a regression chain names (or `None` if the comment is not a chain
comment at all), and `daily_breakdown` rolls per-ticket labels and activity signals up into
a per-day `active`/`chain_active`/`value`/`detected`/`chains` breakdown.

Importing a module that does not exist yet is the deliberate RED for every test in this
file (plan #13 R2: "RED is ModuleNotFoundError") -- `regression_chains.py` is pure,
brand-new, and not created until the implement phase.

Ground truth for the block/table shapes below comes straight from the plan's own worked
examples (plan #13, "Approach" and the R2 table), not guessed:

    | Input                                                        | Expected result                          |
    |---------------------------------------------------------------|-------------------------------------------|
    | Table only                                                     | {acme/demo#3, acme/demo#1, acme/other-repo#47} |
    | Table and an overlapping block                                 | length 3                                  |
    | Heading and an HTML-escaped block only                         | {acme/demo#8, acme/x#9}                   |
    | The same block inside a fence, plus a table row `#8`           | {acme/demo#8, acme/x#9}                   |
    | No heading, but `| #9 |` present                               | None                                       |
    | Header or separator rows                                       | add nothing                               |

R2 also covers one `daily_breakdown` edge case (plan #13, "Approach"): a labelled ticket
whose only comment on a day is plain (no chain heading, no `## Released`, no adev/ato
event block) does not count as active that day.
"""

from __future__ import annotations

from datetime import date

from conftest import CHAIN_HEADING, chain_block, chain_table, escaped, fenced

# `import ecosystem_statistics.regression_chains as regression_chains` (rather than
# `from ecosystem_statistics import regression_chains`) is deliberate: the latter's
# absent-submodule case raises `ImportError: cannot import name ...`, not literally
# `ModuleNotFoundError` -- see test_escalations.py's identical note for plan #11.
import ecosystem_statistics.regression_chains as regression_chains
from ecosystem_statistics.escalations import IssueHistory

REPO = "acme/demo"


# ---------------------------------------------------------------------------
# chain_members: heading gate, table rows, gatekeeper:chain v1 blocks, dedupe
# ---------------------------------------------------------------------------


def test_chain_members_table_only() -> None:
    body = CHAIN_HEADING + "\n\n" + chain_table(["#3", "#1", "acme/other-repo#47"])

    result = regression_chains.chain_members(body, REPO)

    assert result == frozenset({"acme/demo#3", "acme/demo#1", "acme/other-repo#47"})


def test_chain_members_table_and_overlapping_block_dedupes_to_one_set() -> None:
    body = (
        CHAIN_HEADING
        + "\n\n"
        + chain_table(["#3", "#1", "acme/other-repo#47"])
        + "\n"
        + chain_block("acme/demo#3, acme/demo#1, acme/other-repo#47")
    )

    result = regression_chains.chain_members(body, REPO)

    assert len(result) == 3  # union, not table-count + block-count (6)


def test_chain_members_html_escaped_block_only() -> None:
    body = CHAIN_HEADING + "\n\n" + escaped(chain_block("acme/demo#8, acme/x#9"))

    result = regression_chains.chain_members(body, REPO)

    assert result == frozenset({"acme/demo#8", "acme/x#9"})


def test_chain_members_fenced_block_plus_table_row_dedupes() -> None:
    """The same block as above, wrapped in a code fence instead of HTML-escaped, plus a
    bare `#8` table row -- the table row normalises to `acme/demo#8`, already present
    in the block, so the union stays at 2, not 3."""
    body = (
        CHAIN_HEADING
        + "\n\n"
        + chain_table(["#8"])
        + "\n"
        + fenced(chain_block("acme/demo#8, acme/x#9"))
    )

    result = regression_chains.chain_members(body, REPO)

    assert result == frozenset({"acme/demo#8", "acme/x#9"})


def test_chain_members_returns_none_without_the_heading() -> None:
    """A table row alone, with no `## Regression chain (gatekeeper)` heading anywhere in
    the comment, is not a chain comment at all -- `None`, not an empty set."""
    body = chain_table(["#9"])

    result = regression_chains.chain_members(body, REPO)

    assert result is None


def test_chain_members_header_and_separator_rows_add_nothing() -> None:
    body = CHAIN_HEADING + "\n\n| Ticket | Note |\n|---|---|\n"

    result = regression_chains.chain_members(body, REPO)

    assert result == frozenset()


# ---------------------------------------------------------------------------
# daily_breakdown: a labelled ticket's plain-only comment on D does not count as active
# ---------------------------------------------------------------------------


def test_daily_breakdown_plain_comment_does_not_count_as_active() -> None:
    day = date(2024, 3, 5)
    issue = IssueHistory(
        number=1,
        closed_at=None,
        comments=((f"{day.isoformat()}T09:00:00Z", "Just a status update, nothing machine-readable.\n"),),
        labels=("regression-chain",),
    )

    breakdown = regression_chains.daily_breakdown([issue], [day], REPO)

    assert breakdown[day]["active"] == 0
    assert breakdown[day]["value"] is None
