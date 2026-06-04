# nopilot-co-utilities

A Claude Code plugin marketplace for small, **brand-agnostic utilities** — the bits that support the [studios](https://github.com/nopilot-co/nopilot-co-studios) and general day-to-day workflows but aren't creative-production studios themselves.

Each utility is a **self-contained plugin** in its own top-level directory, with a **standalone CLI** that runs without Claude Code. They are maintained together in this one marketplace and installed together by the root `./install.sh` — but each can also be installed on its own.

| Plugin | Standalone CLI | What it does |
|---|---|---|
| **youtube-transcript** | `yt-transcript` | Extract a YouTube video's transcript to a `.txt`. |
| **notion-sources** | `notion-sources` | Extract a Notion database into a batch of per-source `.md` files + a manifest. |
| **source-enrich** | `source-enrich` | Enrich a source batch in place: fetch each source, fill front matter, extract body + assets into an Appendix. |
| **source-summarise** 🚧 | `source-summarise` | _Stub._ Summarise each source — position, core arguments, comment reaction. |
| **theme-cluster** 🚧 | `theme-cluster` | _Stub._ Group sources into themes (core discussion threads). |
| **theme-entity** 🚧 | `theme-entity` | _Stub._ Build a theme entity: synthesis + sourced backlinks by author & timeline. |

## Skills

### youtube-transcript
Extract a YouTube video's transcript to a plain-text `.txt`.

- **Fast path:** fetches existing captions (auto-generated or human) via `youtube-transcript-api` — no API key, no OAuth, no audio download.
- **Fallback (`--fallback`):** downloads the audio with `yt-dlp` and transcribes locally with `faster-whisper` when a video has no captions.

Trigger it by asking naturally (*"get me the transcript of `<url>` as a txt"*), via the standalone CLI, or by invoking the script directly:

```bash
yt-transcript "https://www.youtube.com/watch?v=ID" --out transcript.txt          # after install.sh
python3 youtube-transcript/scripts/extract.py "https://www.youtube.com/watch?v=ID" --out transcript.txt  # from a checkout
```

| Flag | Default | Description |
|------|---------|-------------|
| `--out` | `transcript.txt` | Output file path (use `.md` with front matter/chapters) |
| `--lang` | `en` | Comma-separated language priority, e.g. `de,en` |
| `--paragraph` | off | Collapse caption lines into continuous prose |
| `--fallback` | off | If no captions, download audio and transcribe with faster-whisper |
| `--whisper-model` | `base` | `tiny`\|`base`\|`small`\|`medium`\|`large-v3`\|`turbo` |
| `--keep-audio` | off | Keep the downloaded mp3 instead of using a temp dir |
| `--front-matter` | off | Prepend a YAML block (`url`, `session_title`, `channel`, `duration`, …) |
| `--timestamps` | off | Prefix each line/paragraph with its `[M:SS]` timestamp |
| `--chapters` | off | Split body into `## [M:SS] Title` sections + a `chapters:` list (implies front matter + timestamps) |
| `--chapter-interval` | `5` | Minutes per segment for the fixed-interval chapter fallback |
| `--chapters-json` | — | JSON list of `{time, title}` boundaries to render verbatim |
| `--session-title` | video title | Front-matter `session_title` override |
| `--speaker` | — | Front-matter `speaker` (can't be derived — pass it in) |
| `--event` | — | Front-matter `event` (can't be derived — pass it in) |

Exit codes: `0` success · `2` no captions (re-run with `--fallback`) · `3` error.

**Examples** (use `yt-transcript` after `install.sh`, or `python3 youtube-transcript/scripts/extract.py` from a checkout):

```bash
# Plain captions to a named file
yt-transcript "https://youtu.be/ID" --out talk.txt

# Bare video ID also works; prefer German then English; collapse into prose
yt-transcript dQw4w9WgXcQ --lang "de,en" --paragraph --out talk.txt

# No captions? Fall back to local Whisper (needs yt-dlp on PATH; downloads a model first run)
yt-transcript "https://youtu.be/ID" --fallback --whisper-model small --out talk.txt

# Keep the downloaded audio next to the transcript
yt-transcript "https://youtu.be/ID" --fallback --keep-audio --out talk.txt

# Markdown with YAML front matter + chapters (native YT chapters, else 5-min segments)
yt-transcript "https://youtu.be/ID" --chapters \
  --speaker "Eleanor Dorfman, Head of Industries, Anthropic" --event "SaaStr AI 2026" \
  --out talk.md

# Natural chapters: render exact boundaries you supply (M:SS / H:MM:SS / seconds)
yt-transcript "https://youtu.be/ID" --chapters-json chapters.json --out talk.md
```

A front-matter + chapters run produces, e.g.:

```yaml
---
url: https://www.youtube.com/watch?v=ra0-ZvVApGk
video_id: ra0-ZvVApGk
session_title: How Anthropic's Head of Industries Built an AI-Native Sales Org from Scratch
speaker: Eleanor Dorfman, Head of Industries, Anthropic
event: SaaStr AI 2026
channel: SaaStr AI
duration: 30:40 (1,840s)
language: en
source: captions:en
extracted: 2026-06-04
chapters:
  - time: "00:00"
    title: Introduction & Anthropic's commercial journey
  - time: "03:30"
    title: Hiring for an AI-native sales team
---

## [00:00] Introduction & Anthropic's commercial journey

[00:01] Give a warm welcome to Anthropic's head of industries, Eleanor Dorfman.
…
```

> `session_title` and `channel` are fetched keyless via YouTube's oEmbed endpoint;
> `speaker`/`event` can't be derived, so pass them with `--speaker`/`--event` (in the
> skill, Claude infers them from the transcript when you don't). For *natural*
> chapter titles, supply boundaries via `--chapters-json` — bare `--chapters` only
> uses creator-defined YouTube chapters or fixed-interval segments.

**Behaviour notes:**
- Accepts full URLs (`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/clip/`) or a bare 11-char ID.
- If the requested `--lang` isn't published but other caption tracks exist, it uses the first available track and reports which (`via captions:<lang>`) rather than failing.
- Exit `2` (`NO_CAPTIONS`) means captions are genuinely absent/disabled — re-run with `--fallback`. Real failures (private/removed video, IP block) return exit `3` and are **not** routed into the audio fallback.

### notion-sources

Extract a Notion database into a **batch** of per-source Markdown files plus a
manifest, ready for a downstream batch operation (scrape/enrich/transcribe each
source). One `NNNN-<slug>.md` per row — YAML front matter + a readable stub — alongside
`sources.json` (machine list) and `index.md` (human table).

**Schema-agnostic.** It does not assume column names. Per row it detects the source
URL anywhere (a `url`-typed column, else a URL in the title, else any URL in another
field), maps metadata (`status`, `category`, `tags`, `precis`, `favorite`, `archived`,
`created`, `topics`/`content` relations) by property **type + fuzzy name**, leaves
unknown fields null, and preserves every original column under a `properties:` block —
so it handles differently-structured databases gracefully. Rows with no detectable URL
are skipped and counted. `author`/`author_profile_url` are derived from the URL where
the host exposes it (LinkedIn, X/Twitter, YouTube via oEmbed, Medium, Substack, GitHub).

**Incremental.** Re-running **appends** only sources not already in the batch (dedupe by
Notion page id, URL secondary); existing files and numbering are preserved. `--fresh`
rebuilds from scratch.

Credentials come from the environment (or `--token` / `--database`, or a `.env`):

```bash
export NOPILOT_NOTION_API_KEY=ntn_...
export NOPILOT_NOTION_SOURCE_DATABASE_ID=...        # misspelled …DATABASAE_ID also accepted
notion-sources --out sources/                        # after install.sh
python3 notion-sources/scripts/extract.py --out sources/   # from a checkout
notion-sources --env-file ~/projects/.env --out sources/   # load creds from a .env
```

| Flag | Default | Description |
|------|---------|-------------|
| `--out` | `sources` | Output directory for the batch |
| `--database` | env | Notion database id (else `NOPILOT_NOTION_SOURCE_DATABASE_ID`) |
| `--token` | env | Integration token (else `NOPILOT_NOTION_API_KEY`) |
| `--env-file` | `./.env` | `.env` to load (real env vars take precedence) |
| `--include-archived` | off | Include rows flagged archived |
| `--status NAME` | — | Only rows with this status (repeatable) |
| `--category NAME` | — | Only rows with this category |
| `--favorite` | off | Only rows flagged favorite |
| `--limit N` | `0` (all) | Stop after N new rows (trial runs) |
| `--no-relations` | off | Store relation ids instead of resolving to page titles |
| `--fresh` | off | Rebuild the batch instead of appending |

Exit codes: `0` success · `2` auth/permission failure (bad token or DB not shared with
the integration — nothing written) · `3` error.

A row produces, e.g.:

```yaml
---
id: 371bc247-1b97-81b7-9903-dec6d38c3deb
url: https://www.linkedin.com/posts/fivosaresti_abm-is-the-best-gtm-strategy-...
title: ABM is the best GTM strategy for companies...
author: fivosaresti
author_profile_url: https://www.linkedin.com/in/fivosaresti
status: Unsorted
category: null
tags: []
topics: []
content: []
precis: ""
favorite: false
archived: false
created: 2026-05-31
source_domain: linkedin.com
notion_url: https://www.notion.so/371bc247...
extracted: 2026-06-04T...Z
properties:
  Source: ""
---
```

> The integration must be **shared with the database** (Notion → ⋯ → Connections) or
> the API returns 404/403 and the run exits `2`. The token is read from the environment
> and never printed.

### source-enrich

Enrich a `notion-sources` batch **in place**: for each source it fetches the page,
fully populates the YAML front matter, replaces the stub with the extracted article
body, downloads attached assets, and appends an **Appendix** listing them. Resumable
(skips already-enriched). See `docs/architecture/DECISIONS.md` → **ADR-001** for the
approach.

**Tiered fetch.** A standalone Python engine (`trafilatura`) reads normal pages;
**PDF** (and plaintext) sources are full-text-extracted via `pypdf`; YouTube reuses the
`youtube-transcript` CLI; sources the engine can't read (LinkedIn/X login walls, JS
shells) are flagged **`blocked`** rather than producing garbage. Blocked sources are escalated **politely** — fetch the rendered HTML through
your *own logged-in browser* (`connect-chrome`) or Firecrawl and feed it back via
`--html-file`. It never bypasses authentication.

**Assets.** Inline **content** images + linked documents (pdf/doc/ppt/xls/csv/zip…)
download to `assets/<slug>/` (size-capped, hash-deduped); the Appendix lists Images /
Downloads / Catalogued-not-downloaded with local path + source URL + size. Page chrome
— avatars/profile photos, UI sprites, emoji/icons, tracking pixels, and (on social
posts) commenter pics — is filtered out, and links that resolve to an HTML page rather
than a file are catalogued, not saved.

```bash
source-enrich --batch ~/context/.../research/sources --limit 5     # trial run
python3 source-enrich/scripts/enrich.py --batch path/to/sources    # from a checkout

# escalate one blocked source with HTML from your logged-in browser:
source-enrich --batch path/to/sources --only 42 --html-file /tmp/source-42.html
```

| Flag | Default | Description |
|------|---------|-------------|
| `--batch DIR` | — | Batch directory (holds `sources.json` + `NNNN-*.md`) |
| `--only N\|slug\|id` | — | Enrich only matching sources (repeatable) |
| `--limit N` | all | Stop after N processed (trial runs) |
| `--html-file` / `--md-file` | — | Ingest pre-fetched content (requires one `--only`) |
| `--assets` | `images,docs` | `images,docs` \| `images` \| `none` |
| `--max-asset-mb` | `25` | Per-asset size cap |
| `--reenrich` | off | Re-process already-enriched sources |
| `--delay` | `1.0` | Seconds between network requests (politeness) |
| `--timeout` | `30` | Per-request timeout |
| `--respect-robots` | off | Skip URLs disallowed by `robots.txt` |

Front-matter fields added on enrichment: `enriched`, `enrich_status`
(`enriched`\|`partial`\|`blocked`\|`error`), `enriched_at`, `extractor`,
`http_status`, `source_name`, `published`, `word_count`, `lead_image`,
`assets_count` — plus filled `title`/`author`/`precis`. The Notion `status` field is
left untouched.

Exit codes: `0` ran (per-source failures recorded, not fatal) · `2` bad invocation
(missing batch/manifest, or `--html-file` without one `--only`) · `3` error.

### Thematic evidence base 🚧 (stubs)

`source-summarise`, `theme-cluster`, and `theme-entity` are **stubs** that extend
the pipeline into a **thematic sourced evidence base** for a thought-leadership
conversation:

```
notion-sources → source-enrich → source-summarise → theme-cluster → theme-entity
```

- **source-summarise** — per source: a neutral digest, the author's **position**, the
  **core arguments**, and an assessment of the **comment-section reaction**, written
  into front matter + a `## Core summary` section.
- **theme-cluster** — group sources into **themes** ("contributions to a consistent core
  discussion thread") → `themes.json` (+ optional `themes:` tags on each source).
- **theme-entity** — render a **theme entity** doc per theme: summary, precis, notable
  contributions, key disagreements, comment-reaction assessment, and **backlinks to
  contributing sources grouped by author and by timeline**.

Each follows the **mechanical-CLI + model-supplied-JSON** split (see
`docs/architecture/DECISIONS.md` → **ADR-002**): the CLI does deterministic I/O and
assembly (manifest read, backlinks, author/timeline grouping, rendering); the skill
drives the semantic work (summarising, theming, synthesis) and feeds structured JSON
back. Run any of them with no JSON arg to print its schema + a readiness check. These
are scaffolding — semantic sections render as `_TODO_` until wired.

## Install

The quickest path — clone and run the root installer. It registers the marketplace,
installs every utility plugin, and sets up each standalone CLI (deps + a command on
your PATH):

```bash
git clone https://github.com/nopilot-co/nopilot-co-utilities.git
cd nopilot-co-utilities
./install.sh
```

Or install just the Claude Code plugin (no standalone CLI):

```bash
claude plugin marketplace add nopilot-co/nopilot-co-utilities
claude plugin install youtube-transcript@nopilot-co-utilities
```

Or set up only one utility's standalone CLI from a checkout:

```bash
./youtube-transcript/install.sh        # installs deps + the `yt-transcript` command
# YT_TRANSCRIPT_FALLBACK=1 ./youtube-transcript/install.sh   # also install Whisper-fallback deps
```

Python deps (`youtube-transcript-api`, and for the fallback `yt-dlp` + `faster-whisper`) also auto-install on first use of the plugin skill; see each utility's `requirements.txt`. `yt-dlp` must be on PATH for the fallback (`pip install yt-dlp` or `brew install yt-dlp`).

### Updating

```bash
claude plugin marketplace update nopilot-co-utilities          # refresh the marketplace clone
claude plugin update youtube-transcript@nopilot-co-utilities    # re-copy into the installed cache
```

> **Version bump required.** `claude plugin update` compares the `version` in a plugin's `.claude-plugin/plugin.json` (and its marketplace entry), **not** file contents — so a change to `extract.py` or a skill won't be picked up unless the version is bumped. Bump both the plugin manifest and its `marketplace.json` entry on every shippable change, then run the two commands above and restart Claude Code.

## Layout

The repo root is the **marketplace** (a catalog only). Each utility is a
self-contained plugin in its own top-level directory — its own manifest, skill,
standalone CLI, and installer — mirroring the
[nopilot-co-studios](https://github.com/nopilot-co/nopilot-co-studios) structure.

```
.claude-plugin/
  marketplace.json            # marketplace catalog — lists each utility plugin + its source
install.sh                    # registers marketplace + installs every plugin & CLI
youtube-transcript/           # a utility plugin (source: ./youtube-transcript)
  .claude-plugin/
    plugin.json               # the plugin manifest
  install.sh                  # installs deps + the standalone `yt-transcript` CLI
  skills/
    youtube-transcript/
      SKILL.md                # model-invoked skill; calls scripts/extract.py via $CLAUDE_PLUGIN_ROOT
  scripts/
    extract.py                # the CLI (also runnable standalone)
  requirements.txt
LICENSE                       # MIT
```

## Adding a new utility (build guide)

Every utility in this marketplace **must** follow the same shape so they install
and maintain together. This convention is enforced — see `CLAUDE.md`.

**Required structure** — one top-level directory per utility, named identically to
its plugin (kebab-case):

```
<name>/
  .claude-plugin/
    plugin.json          # "name": "<name>", matching version, homepage .../tree/main/<name>
  install.sh             # installs deps + links a standalone CLI into ~/.local/bin
  skills/
    <name>/
      SKILL.md           # model-invoked; calls the CLI via "$CLAUDE_PLUGIN_ROOT/scripts/..."
  scripts/
    <cli>.py             # standalone, has `#!/usr/bin/env python3`, runnable without Claude Code
  requirements.txt       # pinned/declared Python deps (also pip-installed by install.sh)
```

**Rules (all mandatory):**

1. **Self-contained.** Everything the utility needs lives under `<name>/`. Never
   reach into another utility's directory, and never put utility code at the repo root.
2. **Standalone first.** The core logic is a script that runs on its own
   (`python3 <name>/scripts/<cli>.py …`). The skill is a thin wrapper that calls it
   via `$CLAUDE_PLUGIN_ROOT`; it must not contain logic the CLI lacks.
3. **`install.sh` per utility.** It installs dependencies and links the CLI into
   `~/.local/bin` (idempotent, `set -euo pipefail`). Heavy/optional deps go behind an
   env flag (e.g. `YT_TRANSCRIPT_FALLBACK=1`), not on by default.
4. **Catalog entry.** Add the plugin to `.claude-plugin/marketplace.json` with
   `"source": "./<name>"`. The `name` and `version` there **must** match the plugin's
   own `.claude-plugin/plugin.json`.
5. **Wire the root installer.** Append `<name>` to the `PLUGINS` array in the root
   `install.sh`.
6. **Version discipline.** On any shippable change to a utility, bump the version in
   **both** its `plugin.json` and its `marketplace.json` entry (same number) — see the
   *Version bump required* note above. Use semver: patch for fixes, minor for features,
   major for breaking changes (including a rename of the plugin or its CLI).
7. **Document it.** Add a row to the table at the top of this README and a usage
   section, including flags and exit codes.

**Checklist before committing a new utility:**

```
[ ] <name>/.claude-plugin/plugin.json   (name == dir, version set, homepage points to /tree/main/<name>)
[ ] <name>/install.sh                    (executable, idempotent, links CLI to ~/.local/bin)
[ ] <name>/scripts/<cli>.py              (executable shebang, runs standalone)
[ ] <name>/skills/<name>/SKILL.md        (calls CLI via $CLAUDE_PLUGIN_ROOT)
[ ] <name>/requirements.txt
[ ] marketplace.json entry               (source ./<name>; name+version match plugin.json)
[ ] root install.sh PLUGINS array        (includes <name>)
[ ] README                               (table row + usage section)
[ ] verified: bash -n on both install.sh; ran <name>/install.sh; ran the CLI end-to-end
```

## License

MIT — see [LICENSE](LICENSE).
