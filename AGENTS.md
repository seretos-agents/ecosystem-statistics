<!-- AGENTS.md authoring rule (keep this comment in the template; delete it in a real repo):
     Document ONLY what an agent cannot derive by reading the code and the file tree.
     - DO capture: cross-file / cross-repo contracts, non-obvious conventions, gotchas and
       their "why", external requirements, and deliberate design choices.
     - DON'T restate: the directory layout, what a workflow YAML does step-by-step, or how a
       build script works line-by-line — an agent reads those directly. If a sentence only
       narrates a file the reader already has in front of them, cut it.
     A lean AGENTS.md the agent trusts beats an exhaustive one it has to re-verify. -->

# ecosystem-statistics — agent guide

A deterministic nightly collector that reads GitHub (issues, comments, PRs, commits) across the
Modular Software Factory ecosystem and writes daily JSON snapshots, so a frontend can chart
quality/throughput trends without ever touching the GitHub API itself.

## Tool priority

Tool priority: see the ecosystem root AGENTS.md — skills and MCP tools before raw file tools.

## Working on a ticket

Ticket work is driven by the ecosystem orchestrator (`agent-ticket-orchestrator`), which prepares
the worktree and branch and dispatches the work package. It is never done by hand on `main`.

## Non-obvious constraints

- **The collector itself must stay deterministic — no LLM call, no Claude Code session.** It is a
  plain Python script invoked by a scheduled GitHub Action. The *tickets that build it* go through
  the usual AI pipeline; the *artifact it produces* must not.
- **Two branches, two roles:**
  - `main` — code, PR-only (branch protection), reviewed like any other repo in the ecosystem.
  - `data` — output only. The nightly workflow commits JSON straight to `data` (no PR, no review —
    it's generated data, not code) and never touches `main`. A frontend fetches JSON from `data` via
    `raw.githubusercontent.com` or an equivalent raw endpoint.
- **All days are UTC calendar days.** `--since`/`--until` and the on-disk `daily/YYYY-MM-DD.json`
  filenames are UTC dates, not local time and not GitHub's ambient timezone in any UI.
- **Output must be idempotent and reproducible.** The same `--since`/`--until` window must produce
  byte-identical JSON on a re-run (sorted keys, no `generated_at`/wall-clock fields inside a daily
  file) — the nightly workflow relies on this to skip empty commits.
- **The collector must accept an arbitrary date range, not just "yesterday".** History predates this
  repo; a backfill run and the nightly run share the same code path, only the `--since`/`--until`
  window differs.
- **Tokens:** `ECOSYSTEM_TOKEN` (classic PAT, `repo`+`project` scope) reads across the org — a
  fine-grained PAT cannot see Projects. The nightly workflow's push to `data` uses the run's own
  `GITHUB_TOKEN` with `permissions: contents: write`, scoped to this repo only.
- **Repo scope lives in `config/repos.yml`, not in code.** It currently covers only
  `seretos-agents/*`. Adding a repo (e.g. later widening to `Seretos/*`) is a config change, not a
  code change.
- **The pipeline's own traces are the data source — see the parser ticket for the exact formats.**
  In short: `adev:event v1` HTML-comment blocks on tickets carry critic/review/CI round counts (two
  gate vocabularies: the developer lane and the prose lane); gatekeeper comments are headed
  `## Released`, `## Frame`, `## Regression chain`, `## Clarification needed`, `## Lane split`,
  `## Re-cut`; escalation to a human has **no dedicated label or event** — it's a freehand
  `Escalated: …` comment plus the card landing in the `Question` board column. The GitHub *author*
  of every one of these is always the human account whose token ran the pipeline (`Seretos`) — never
  classify machine vs. human by author or by the `#ai-generated` prefix, both are present on human
  replies too. Classify by comment heading / HTML-comment block instead.
- **PRs merge as real two-parent merge commits**, not squashes. Feature-branch churn (critic/review
  rework) is computed via `git diff <merge>^1 <merge>^2` vs. the sum of each branch commit's own
  diff — the gap between "net lines added" and "gross lines added" is the churn signal.
- **AI attribution:** the project-issues MCP automatically prefixes every comment and PR body with
  `#ai-generated`. Never type that prefix yourself.
