---
name: youtube-transcript
description: Extract the transcript of a YouTube video to a plain-text .txt (or Markdown) file. Use when the user gives a YouTube URL or video ID and wants the transcript, captions, subtitles, or a text version of what was said — including a version with YAML front matter (url, title, speaker, event, duration), per-line timestamps, or the talk chunked into chapters/sections with a timestamped chapter listing. Handles videos that already have captions (fast path) and falls back to local Whisper audio transcription when they don't.
---

# YouTube Transcript Extractor

Extract a YouTube video's transcript to a `.txt` file.

## Inputs
- A YouTube URL (`https://www.youtube.com/watch?v=ID`, `https://youtu.be/ID`, or a Shorts/clip URL) **or** a bare 11-character video ID.
- Optional: target language code(s) (default `en`), output path (default `transcript.txt` in the cwd).

## Procedure

### 1. Fast path — existing captions
Run the bundled script. It accepts a URL or ID, extracts the ID itself, and writes plain text:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/extract.py" "<url-or-id>" --out transcript.txt --lang en
```

`$CLAUDE_PLUGIN_ROOT` is set by Claude Code to this plugin's root. The script auto-installs `youtube-transcript-api` if missing. On success it prints the output path and word count — report those to the user and offer the file.

### 2. Fallback — no captions
If step 1 exits code **2** printing `NO_CAPTIONS`, the video has no captions and must be transcribed from audio. This is built into the script behind the `--fallback` flag.

**Confirm with the user first** — it downloads the audio (via `yt-dlp`) and, on first use, a Whisper model (hundreds of MB), then runs CPU transcription (minutes for a long video). Once confirmed, re-run with `--fallback`:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/extract.py" "<url-or-id>" --out transcript.txt --fallback --whisper-model base
```

- Requires `yt-dlp` on PATH (`pip install yt-dlp` or `brew install yt-dlp`); `faster-whisper` auto-installs on first run.
- `--whisper-model` sizes: `tiny|base|small|medium|large-v3|turbo`. `base` is a good speed/quality default; use `small`/`medium` for better accuracy at more time.
- `--keep-audio` leaves the downloaded mp3 in the cwd (default: deleted from a temp dir).
- For a cloud alternative (OpenAI Whisper / Deepgram / AssemblyAI), download audio with `yt-dlp -x --audio-format mp3` and send it to the user's configured API — only if they have a key and prefer it over local.

### 3. Front matter, timestamps & chapters (optional)
When the user wants more than raw text — a header block, timestamps, or the talk
broken into sections — layer these flags on top of either path above.

**Front matter.** `--front-matter` prepends a YAML block. The script fills the
mechanical fields itself: `url`, `video_id`, `session_title` + `channel`
(fetched keyless via YouTube oEmbed), `duration` (`30:40 (1,840s)`), `language`,
`source`, `extracted`. Two fields it **cannot** derive — pass them in:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/extract.py" "<url-or-id>" --out talk.md \
  --front-matter \
  --session-title "How Anthropic's Head of Industries Built an AI-Native Sales Org from Scratch" \
  --speaker "Eleanor Dorfman, Head of Industries, Anthropic" \
  --event "SaaStr AI 2026"
```

If the user doesn't supply `--speaker`/`--event`, **infer them from the transcript
content** (the speaker usually introduces themselves in the first minute; the event
is often named in the intro or the video title) and pass your inference in — don't
leave them blank if the talk makes them clear. `--session-title` defaults to the
real YouTube title, so only override it when the user wants a cleaner one.

**Timestamps.** `--timestamps` prefixes every line (or paragraph, with
`--paragraph`) with its `[M:SS]` mark.

**Chapters.** `--chapters` splits the body into `## [M:SS] Title` sections and adds
a `chapters:` list to the front matter (implies `--front-matter` + `--timestamps`).
Chapter source, in priority:
1. **`--chapters-json FILE`** — render exact boundaries you supply. **This is the
   route for *natural* chapters.** First produce a timestamped transcript
   (`--timestamps`, no `--chapters`), read it, decide ~4–10 sensible section
   boundaries with descriptive titles, then write a small JSON and re-run:
   ```json
   [ {"time": "0:00",  "title": "Introduction & Anthropic's commercial journey"},
     {"time": "3:30",  "title": "Hiring for an AI-native sales team"},
     {"time": "12:15", "title": "Tooling and workflow automation"} ]
   ```
   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/extract.py" "<url-or-id>" --out talk.md \
     --chapters-json chapters.json --speaker "…" --event "…"
   ```
   `time` accepts `M:SS`, `H:MM:SS`, or raw seconds.
2. **Native YouTube chapters** — if the creator defined them and `yt-dlp` is on
   PATH, `--chapters` uses them verbatim.
3. **Fixed-interval fallback** — otherwise `--chapters` segments every
   `--chapter-interval` minutes (default 5), titled by timestamp. Mechanical only;
   prefer the `--chapters-json` route when you want meaningful titles.

Write the output to a `.md` file when using front matter/chapters (it's Markdown).

## Notes
- Use `--lang "de,en"` to try languages in priority order.
- Use `--paragraph` to collapse caption lines into continuous prose instead of one line per caption.
- The YouTube Data API `captions.download` endpoint is intentionally **not** used — it needs OAuth and video ownership for most tracks.
- Front-matter metadata (title/channel) comes from the keyless oEmbed endpoint; it's best-effort and silently skipped if the network is unavailable.
