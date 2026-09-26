# ecosystem-statistics

A deterministic nightly collector for the Modular Software Factory ecosystem.

Every night, a GitHub Action (no LLM, no Claude Code session — plain, deterministic Python)
scans the pipeline's own traces on GitHub across the ecosystem's repos — ticket comments, PR
metadata, commit history — and writes one JSON snapshot per UTC day to the `data` branch. A
frontend can then `fetch()` those JSON files directly (e.g. via `raw.githubusercontent.com`)
and render charts, with no server and no GitHub API calls of its own.

## Why

The Modular Software Factory pipeline (`agent-ticket-orchestrator` + `agent-autonomous-developer`
and friends) already leaves a rich, structured trail behind on every ticket and PR it touches:
critic/review/CI round counts, escalations to a human, regression chains, plan re-cuts, and more.
This repo turns that trail into a queryable time series, to track things like:

- **Line-level churn**, split by feature-branch rework (critic/reviewer catching things before
  merge — good) vs. main-branch rework (bugs slipping past review — bad).
- **Escalation rate**: how many in-progress tickets needed a human decision.
- **Average critic/review/CI rounds** per ticket, and how often a package needed a full replan.
- **Regression-chain share**: how many of today's active tickets are the latest attempt at a
  symptom that survived earlier fixes.

See `AGENTS.md` for the constraints an implementing agent needs to know, and the tickets in this
repo's tracker for the concrete build-out (collector → parser → individual metrics → nightly
workflow).

## Data

- **Code lives on `main`** (PR-only, reviewed like any other repo).
- **Data lives on `data`** — the nightly workflow commits straight there, no PR. Each day is
  `daily/YYYY-MM-DD.json` (UTC), plus an `index.json` listing every day collected so far.
- Runs are idempotent: re-collecting the same window reproduces byte-identical JSON.

## Nightly publish

`.github/workflows/nightly.yml` runs `python -m ecosystem_statistics publish` every night
at 03:17 UTC, collecting and publishing the previous UTC calendar day to `data`. It commits
and pushes only when the collected output actually differs from what's already there.

To backfill past days, dispatch the workflow manually (Actions tab → `nightly` → *Run
workflow*) with `since`/`until` inputs (`YYYY-MM-DD`, both required together) for the
window to collect. Fire dispatches one at a time -- see the `concurrency` comment in
`nightly.yml` for why a burst of several in a row can drop all but the last one.

## Development

```bash
python -m pip install -e ".[test]"
python -m pytest
```
