---
name: source-enrich
description: Enrich a notion-sources batch — fetch each source, fully populate its YAML front matter, extract the article body to Markdown, download attached assets (inline images + linked documents) into assets/<slug>/, and append an Appendix listing them. Use when the user wants to enrich/expand/scrape/"fetch the bodies of" a source batch produced by notion-sources, or asks to fill in front matter and pull article content + downloads for a folder of source .md files. Tiered fetch: a standalone Python engine for normal pages, youtube-transcript for YouTube, and an escalation path (the user's logged-in browser via connect-chrome, or Firecrawl) for auth-walled/JS sources like LinkedIn and X that come back "blocked". Resumable; enriches in place.
---

# Source Enricher

Enrich a `notion-sources` batch in place: fetch each source, fully populate its
front matter, extract the article body, download assets, and append an Appendix.
See **ADR-001** (`docs/architecture/DECISIONS.md`) for the locked approach.

## Procedure

### 1. Run the enricher over the batch
```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/enrich.py" --batch <batch-dir> --limit 5
```
`<batch-dir>` is the directory holding `sources.json` + the `NNNN-*.md` files
(e.g. `~/context/context-message-nopilot/research/sources`). The script needs
`trafilatura` + `PyYAML` (installed by this utility's `install.sh`). It is
**resumable** — already-enriched files are skipped unless `--reenrich`.

**Always trial with `--limit` first** on an unfamiliar batch, then widen. Keep the
default `--delay` (politeness) when running the full set.

On finish it prints per-status counts and a **`blocked` list** — those are the
sources the Python engine couldn't read (login walls / JS). Report the summary.

### 2. What it does per source
- **Fetch:** Python `trafilatura` for normal pages; **PDF/plaintext** sources are
  full-text-extracted (`pypdf`); YouTube reuses `yt-transcript` if installed;
  otherwise the page is fetched and extracted to Markdown.
- **Front matter:** fills nulls (`title`, `author`, `precis`, `published`,
  `source_name`, `lead_image`, `word_count`) and adds `enriched`, `enrich_status`,
  `enriched_at`, `extractor`, `http_status`, `assets_count`. It never overwrites the
  Notion `status` field.
- **Body:** the stub is replaced with the extracted article.
- **Assets:** inline images + linked documents (pdf/doc/ppt/xls/csv/zip…) download
  to `assets/<slug>/` (size-capped, hash-deduped); a `## Appendix — Assets` section
  lists Images / Downloads / Catalogued.
- **Status:** `enriched` · `partial` (body but assets failed) · `blocked` (couldn't
  fetch) · `error`.

### 3. Escalate blocked sources (LinkedIn, X, JS-walled) — politely
Blocked sources are **not** scraped around their login. To enrich them, fetch the
rendered HTML through a session the user is entitled to, then feed it back in:

1. Get the page HTML via the user's **own logged-in browser** — use the
   `connect-chrome` skill (their cookies) or the `scrape`/Firecrawl skills — and
   save it to a temp `.html` (or save the article text to a `.md`).
2. Re-run for just that source:
   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/enrich.py" --batch <batch-dir> \
     --only <N> --html-file /tmp/source-<N>.html
   ```
   (`--only` matches the row number `N` (or zero-padded `0042`), the full slug,
   the Notion id, or the filename — exact matches, repeatable. Use `--md-file`
   instead if you already have clean Markdown.)

Confirm with the user before driving their browser or using a paid service, and
before a large run. Never attempt to bypass authentication or rate limits.

### 4. Useful flags
| Flag | Default | Effect |
|------|---------|--------|
| `--only N\|slug\|id` | — | Enrich only matching sources (repeatable) |
| `--limit N` | all | Stop after N processed (trial runs) |
| `--html-file` / `--md-file` | — | Ingest pre-fetched content (needs one `--only`) |
| `--assets images,docs\|images\|none` | `images,docs` | What to download |
| `--max-asset-mb` | `25` | Per-asset size cap |
| `--reenrich` | off | Re-process already-enriched sources |
| `--delay` | `1.0` | Seconds between network requests (politeness) |
| `--respect-robots` | off | Skip URLs disallowed by robots.txt |

Exit codes: `0` ran (per-source failures recorded, not fatal) · `2` bad invocation
(missing batch/manifest, or `--html-file` without a single `--only`) · `3` error.
