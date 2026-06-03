---
name: notion-sources
description: Extract a Notion database into a batch of per-source Markdown files (one .md per row, with YAML front matter) plus a manifest (sources.json + index.md), ready for a downstream batch utility to iterate. Use when the user wants to pull a Notion database / source list / link database / read-later capture into local files, build or top-up a "source batch", or export Notion rows as Markdown with front matter. Schema-agnostic — detects the source URL anywhere in each row and maps metadata (status, category, tags, precis, favorite, created, topics/content relations) by property type + fuzzy name, so it handles differently-structured Notion databases. Derives author/author_profile_url from the URL. Re-runs append new sources to the existing batch.
---

# Notion Sources Extractor

Pull a Notion database into a **batch**: one `NNNN-<slug>.md` per row (YAML front
matter + a readable stub) plus a manifest — `sources.json` (machine list for the
next utility) and `index.md` (human table). Built to feed a downstream batch
operation (scrape/enrich/transcribe each source).

## Credentials
The script reads auth from the environment (never pass secrets on the command line
if avoidable):
- `NOPILOT_NOTION_API_KEY` — Notion integration token (also `NOTION_API_KEY` / `NOTION_TOKEN`)
- `NOPILOT_NOTION_SOURCE_DATABASE_ID` — database id (also the misspelled
  `NOPILOT_NOTION_SOURCE_DATABASAE_ID`, and `NOTION_DATABASE_ID`)

A `.env` in the cwd is auto-loaded; for another location pass `--env-file <path>`.
If the user keeps these in `~/projects/.env`, run with `--env-file ~/projects/.env`.

## Procedure

### 1. Run the extractor
```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/extract.py" --out sources/
```
`$CLAUDE_PLUGIN_ROOT` is set by Claude Code to this plugin's root. The script needs
no third-party packages. On success it prints how many sources were added, how many
were skipped (already in batch / filtered / no URL), and the manifest + index paths.
Report those to the user.

**Confirm scope first for a large or unknown database** — offer `--limit 5` for a
trial run so the user can eyeball the output before a full pull.

### 2. Schema-agnostic behaviour (what to expect)
The extractor does **not** assume column names, so it works across databases:
- **URL detection is primary** — it uses a `url`-typed column if present, else a URL
  embedded in the title, else any URL found in another field. Rows with **no**
  detectable URL are skipped and counted (reported at the end).
- Metadata is matched by **property type + fuzzy name** (e.g. a `status` column, a
  multi-select that looks like tags, a rich-text that looks like a summary). When a
  database lacks a field, that front-matter key is simply null/empty.
- **Every original column is preserved** verbatim under a `properties:` block, so no
  data is lost even when the schema differs from the expected one.

### 3. Filters & incremental top-up (optional flags)
| Flag | Effect |
|------|--------|
| `--include-archived` | include rows flagged archived (default: excluded) |
| `--status NAME` | only rows with this status (repeatable) |
| `--category NAME` | only rows with this category |
| `--favorite` | only rows flagged favorite |
| `--limit N` | stop after N new rows (trial runs) |
| `--no-relations` | store relation ids instead of resolving them to page titles |
| `--fresh` | rebuild the batch from scratch instead of appending |

Re-running **appends** only sources not already in the batch (dedupe by Notion page
id, URL as secondary key); existing files and numbering are preserved. Use `--fresh`
only when the user wants a clean rebuild.

### 4. Enrich blank authors (optional, model-in-the-loop)
`author` / `author_profile_url` are derived from the source URL for hosts that expose
it (LinkedIn, X/Twitter, YouTube via oEmbed, Medium, Substack, GitHub). For other
hosts they're left `null`. If the user wants them filled, read the relevant source
files' front matter, infer the author from the URL/title/precis where you reasonably
can, and update the `author:` / `author_profile_url:` fields (and the matching entry
in `sources.json`). Don't fabricate — leave `null` when genuinely unknown.

## Output shape
```
sources/
  sources.json        # {database_id, generated, count, sources:[{n,id,file,url,title,author,status,…}]}
  index.md            # human table of the whole batch
  0001-<slug>.md      # per-source: YAML front matter (id, url, title, author,
  0002-<slug>.md      #   author_profile_url, status, category, tags, topics,
  …                   #   content, precis, favorite, archived, created,
                      #   source_domain, notion_url, extracted, properties:{…})
```

Exit codes: `0` success · `2` auth/permission failure (bad token or DB not shared
with the integration — nothing written) · `3` other error.
