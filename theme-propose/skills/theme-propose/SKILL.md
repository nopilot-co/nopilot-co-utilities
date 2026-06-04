---
name: theme-propose
description: STUB. Non-destructive theme-framework proposal for a summarised source batch, plus the governing theme manifest. Use when the user wants to propose/agree a set of themes BEFORE clustering, define theme priorities and an editorial approach per theme, or establish categorisation guidance that governs all later theme work. Builds a read-only digest of the summarised sources, materialises a model-proposed framework (theme-proposal.md/.json) for the user to agree, and freezes it into theme-manifest.json. Never edits source files.
---

# Theme Proposer (STUB)

Propose a **theme framework** for a summarised batch and freeze the agreed
version into a governing **theme manifest** — the step before clustering:
`source-summarise → theme-propose → theme-cluster → theme-entity`. See ADR-002.

> **Non-destructive.** This only reads sources and writes proposal/manifest
> files. It never edits the source `.md` files.

## Procedure

### 1. Scan (read-only) → context digest
```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/propose.py" --batch <batch-dir>
```
Writes `theme-context.json` — a compact digest (n, title, author, date,
position, summary) of every enriched/summarised source. (Run `source-summarise`
first so positions/summaries exist.)

### 2. Propose a framework (model)
First, **load the ICP** from the project's messaging house (e.g.
`messaging/people.md`, or the equivalent ICP/audience definition) — this is the
"so what" anchor and is project judgment that does NOT live in the plugin. Carry it
as data: set `icp` + `icp_source`, and give every theme a `so_what` (why it matters
to the ICP). Then read `theme-context.json` and propose a coherent theme framework —
themes that are genuine *core discussion threads*, with priorities, a per-theme
editorial approach, and the `so_what`. Emit proposal JSON (schema in the script
docstring / `--help`):
```json
{
  "editorial": "overall angle, purpose, and audience of the evidence base",
  "categorisation_guidance": "how sources should be assigned (multi-membership? thresholds? what to exclude)",
  "themes": [
    { "id": "agentic-gtm", "label": "Agentic GTM", "description": "...",
      "rationale": "why this is one coherent thread", "priority": 1,
      "editorial_approach": "the angle/argument this theme's dossier should foreground",
      "inclusion_criteria": "what qualifies a source for this theme",
      "candidate_members": ["<n|file>", ...] }
  ],
  "unthemed": ["<n>", ...]
}
```
Materialise it for review:
```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/propose.py" --batch <batch-dir> --proposal-json proposal.json
```
Writes `theme-proposal.md` (human-editable) + `theme-proposal.json`. **Present
`theme-proposal.md` to the user and get agreement on priorities + editorial
approach.** Apply their edits to the JSON.

### 3. Adopt → freeze the governing manifest
```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/propose.py" --batch <batch-dir> --adopt theme-proposal.json
```
Writes `theme-manifest.json` — editorial, categorisation guidance, ordered
priorities, and per-theme editorial_approach + inclusion_criteria. **All
subsequent `theme-cluster` and `theme-entity` runs read this manifest** so
categorisation and editorial voice stay consistent across runs.

Exit codes: `0` ok · `2` missing batch/manifest · `3` error.
