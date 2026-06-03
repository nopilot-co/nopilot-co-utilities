#!/usr/bin/env python3
# Part of the nopilot-co-utilities Claude Code plugin (utilities:youtube-transcript).
# Invoked by skills/youtube-transcript/SKILL.md via $CLAUDE_PLUGIN_ROOT/scripts/extract.py.
# Also runnable standalone: python3 scripts/extract.py <url-or-id>
"""Extract a YouTube transcript to a plain-text (or Markdown) file.

Fast path: fetch existing captions via youtube-transcript-api.
Fallback (opt-in, --fallback): download audio with yt-dlp and transcribe
locally with faster-whisper when no captions exist.

Optionally emit YAML front matter (--front-matter), per-line timestamps
(--timestamps), and chapter sections (--chapters). The script renders these
mechanically; semantic fields it can't derive (speaker, event) are passed in
via --speaker/--event, and natural chapter boundaries can be supplied via
--chapters-json (e.g. by a model that has read the timestamped transcript).

Usage:
    extract.py <url-or-id> [--out transcript.txt] [--lang en] [--paragraph]
                           [--fallback] [--whisper-model base] [--keep-audio]
                           [--front-matter] [--timestamps]
                           [--chapters] [--chapter-interval 5]
                           [--chapters-json chapters.json]
                           [--session-title "..."] [--speaker "..."] [--event "..."]

Exit codes:
    0  success
    2  no captions available AND fallback not requested (caller may re-run
       with --fallback after confirming with the user)
    3  other error
"""
import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request


def extract_video_id(s: str) -> str:
    """Accept a full URL or a bare ID and return the 11-char video ID."""
    s = s.strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s
    m = re.search(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/|/clip/)([0-9A-Za-z_-]{11})", s)
    if m:
        return m.group(1)
    raise ValueError(f"Could not extract a video ID from: {s!r}")


def pip_install(pkg: str):
    print(f"Installing {pkg} ...", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pkg], check=True)


# --------------------------------------------------------------- time helpers
def hms(seconds) -> str:
    """Seconds -> 'M:SS' (or 'H:MM:SS' past an hour)."""
    sec = int(round(float(seconds)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_timecode(v) -> float:
    """Accept seconds (int/float/str) or 'M:SS' / 'H:MM:SS' -> seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", v):
        return float(v)
    parts = v.split(":")
    if not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"Bad timecode: {v!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def duration_str(total_seconds) -> str:
    """Total seconds -> '30:40 (1,840s)' to match the documented schema."""
    total = int(round(float(total_seconds)))
    return f"{hms(total)} ({total:,}s)"


# ----------------------------------------------------------------- YAML output
def _yaml_scalar(s) -> str:
    """Render a string as a YAML scalar, quoting only when necessary."""
    s = str(s)
    needs_quote = (
        s == ""
        or s != s.strip()
        or ": " in s
        or s.endswith(":")
        or s[0] in ">|@`\"'#&*!?-[]{},"
        or "#" in s
        or "\n" in s
    )
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def build_front_matter(fields, chapters) -> str:
    """fields: list of (key, value) with value already a plain string (skip None).
    chapters: list of {"start": seconds, "title": str} or None."""
    lines = ["---"]
    for key, value in fields:
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_yaml_scalar(value)}")
    if chapters:
        lines.append("chapters:")
        for ch in chapters:
            lines.append(f'  - time: "{hms(ch["start"])}"')
            lines.append(f"    title: {_yaml_scalar(ch['title'])}")
    lines.append("---")
    return "\n".join(lines)


# --------------------------------------------------------------- body renderer
def _norm(text: str) -> str:
    """Collapse internal whitespace/newlines in a single caption cue."""
    return " ".join(text.split())


def _group_paragraphs(cues, max_words: int = 60, gap: float = 2.5):
    """Group cues into paragraphs on a word-count cap or a silence gap."""
    paras, cur, cur_words, last_end = [], [], 0, None
    for c in cues:
        gap_now = last_end is not None and (c["start"] - last_end) > gap
        if cur and (cur_words >= max_words or gap_now):
            paras.append(cur)
            cur, cur_words = [], 0
        cur.append(c)
        cur_words += len(c["text"].split())
        last_end = c["start"] + c.get("dur", 0.0)
    if cur:
        paras.append(cur)
    return paras


def render_cues(cues, paragraph: bool, timestamps: bool) -> str:
    """Render a flat list of cues (one chapter's worth, or the whole talk)."""
    if not cues:
        return ""
    if paragraph:
        out = []
        for para in _group_paragraphs(cues):
            text = " ".join(_norm(c["text"]) for c in para).strip()
            if timestamps:
                text = f"[{hms(para[0]['start'])}] {text}"
            out.append(text)
        return "\n\n".join(out)
    lines = []
    for c in cues:
        if timestamps:
            lines.append(f"[{hms(c['start'])}] {_norm(c['text'])}")
        else:
            lines.append(c["text"])
    return "\n".join(lines)


def render_body(cues, chapters, paragraph: bool, timestamps: bool) -> str:
    """Render the transcript body, optionally split into chapter sections."""
    if not chapters:
        # Legacy plain/paragraph output (no new flags): one continuous prose
        # block for --paragraph, one line per cue otherwise — unchanged.
        if paragraph and not timestamps:
            return " ".join(_norm(c["text"]) for c in cues).strip()
        return render_cues(cues, paragraph, timestamps)

    bounds = sorted(chapters, key=lambda c: c["start"])
    sections = []
    for i, ch in enumerate(bounds):
        start = ch["start"]
        end = bounds[i + 1]["start"] if i + 1 < len(bounds) else float("inf")
        chunk = [c for c in cues if start <= c["start"] < end]
        header = f"## [{hms(start)}] {ch['title']}"
        body = render_cues(chunk, paragraph, timestamps)
        sections.append(f"{header}\n\n{body}".rstrip())
    return "\n\n".join(sections)


# ------------------------------------------------------------------- metadata
def fetch_oembed(video_id: str):
    """Best-effort title/author via YouTube's keyless oEmbed endpoint.

    Prefers `requests` (bundles certifi, and is already pulled in by
    youtube-transcript-api) so TLS verification works on stock Python builds
    that lack a system cert bundle; falls back to urllib otherwise."""
    url = (
        "https://www.youtube.com/oembed?format=json&url="
        f"https://www.youtube.com/watch?v={video_id}"
    )
    try:
        import requests  # noqa
        return requests.get(url, timeout=10,
                            headers={"User-Agent": "yt-transcript"}).json()
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "yt-transcript"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def fetch_ytdlp_info(video_id: str):
    """Best-effort metadata + native chapters via yt-dlp (if installed)."""
    if not shutil.which("yt-dlp"):
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings", "--dump-single-json", url],
            check=True, capture_output=True, text=True,
        )
        return json.loads(out.stdout)
    except Exception:
        return {}


# --------------------------------------------------------------------- chapters
def load_chapters_json(path: str):
    """Load model/human-supplied chapter boundaries.

    Accepts either [{"time": "3:12", "title": "..."}, ...] or
    {"chapters": [...]}. Each entry may use "time"/"start"/"timestamp"."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("chapters", [])
    chapters = []
    for entry in data:
        tc = entry.get("time", entry.get("start", entry.get("timestamp")))
        title = entry.get("title", entry.get("name", ""))
        if tc is None:
            continue
        chapters.append({"start": parse_timecode(tc), "title": str(title).strip()})
    return sorted(chapters, key=lambda c: c["start"])


def native_chapters(info: dict):
    """Creator-defined YouTube chapters from yt-dlp info, if any."""
    out = []
    for ch in (info or {}).get("chapters") or []:
        if ch.get("start_time") is None:
            continue
        out.append({"start": float(ch["start_time"]), "title": (ch.get("title") or "").strip()})
    return sorted(out, key=lambda c: c["start"])


def interval_chapters(total_seconds: float, interval_min: float):
    """Mechanical fallback: fixed-length segments labelled by their start time."""
    step = max(1.0, interval_min * 60.0)
    out, t, n = [], 0.0, 1
    while t < max(total_seconds, 1.0):
        out.append({"start": t, "title": f"Segment {n} ({hms(t)})"})
        t += step
        n += 1
    return out


# ------------------------------------------------------------------- captions
def fetch_captions(video_id: str, langs):
    """Fetch caption cues for a video.

    Returns (cues, lang_code) where cues is a list of
    {"start": float, "dur": float, "text": str}, or None when the video
    genuinely has no captions. Real failures propagate as exceptions.
    """
    try:
        import youtube_transcript_api  # noqa: F401
    except ImportError:
        pip_install("youtube-transcript-api")
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

    api = YouTubeTranscriptApi()

    def to_cues(transcript):
        return [
            {"start": float(s.start), "dur": float(s.duration), "text": s.text}
            for s in transcript
        ]

    # Preferred-language fast path.
    try:
        transcript = api.fetch(video_id, languages=langs)
        return to_cues(transcript), (langs[0] if langs else "?")
    except TranscriptsDisabled:
        return None  # captions turned off -> genuine no-captions
    except NoTranscriptFound:
        pass  # requested language(s) absent — other tracks may exist; check below

    try:
        available = list(api.list(video_id))
    except TranscriptsDisabled:
        return None
    if not available:
        return None
    chosen = available[0]
    return to_cues(chosen.fetch()), chosen.language_code


def whisper_fallback(video_id: str, model_size: str, keep_audio: bool):
    """Download audio with yt-dlp and transcribe with faster-whisper.

    Returns a list of cue dicts (with timestamps)."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError(
            "yt-dlp not found. Install it (pip install yt-dlp, or brew install yt-dlp) "
            "to use the --fallback audio-transcription path."
        )
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        pip_install("faster-whisper")
    from faster_whisper import WhisperModel

    url = f"https://www.youtube.com/watch?v={video_id}"
    workdir = "." if keep_audio else tempfile.mkdtemp(prefix="yt_audio_")
    audio_path = os.path.join(workdir, f"{video_id}.mp3")
    out_tmpl = os.path.join(workdir, f"{video_id}.%(ext)s")

    print(f"No captions — downloading audio for {video_id} ...", file=sys.stderr)
    subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "-o", out_tmpl, url],
        check=True,
    )

    print(f"Transcribing with faster-whisper ({model_size}, cpu/int8) ...", file=sys.stderr)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, beam_size=5)
    print(f"Detected language '{info.language}' "
          f"(p={info.language_probability:.2f}, {info.duration:.0f}s audio)", file=sys.stderr)

    cues = [
        {"start": float(seg.start), "dur": float(seg.end - seg.start), "text": seg.text.strip()}
        for seg in segments
    ]

    if not keep_audio:
        shutil.rmtree(workdir, ignore_errors=True)
    return cues


def total_duration(cues) -> float:
    if not cues:
        return 0.0
    last = cues[-1]
    return last["start"] + last.get("dur", 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="YouTube URL or 11-char video ID")
    ap.add_argument("--out", default="transcript.txt")
    ap.add_argument("--lang", default="en", help="comma-separated language priority, e.g. 'de,en'")
    ap.add_argument("--paragraph", action="store_true",
                    help="collapse caption lines into continuous prose")
    ap.add_argument("--fallback", action="store_true",
                    help="if no captions, download audio and transcribe with faster-whisper")
    ap.add_argument("--whisper-model", default="base",
                    help="faster-whisper size: tiny|base|small|medium|large-v3|turbo (default base)")
    ap.add_argument("--keep-audio", action="store_true",
                    help="keep the downloaded mp3 in the cwd instead of a temp dir")
    # ----- front matter & structure
    ap.add_argument("--front-matter", action="store_true",
                    help="prepend a YAML front-matter block (url, title, duration, ...)")
    ap.add_argument("--timestamps", action="store_true",
                    help="prefix each line/paragraph with its [M:SS] timestamp")
    ap.add_argument("--chapters", action="store_true",
                    help="split the body into chapter sections (native YouTube chapters if "
                         "available, else fixed --chapter-interval segments); implies front matter")
    ap.add_argument("--chapter-interval", type=float, default=5.0,
                    help="minutes per segment for the fixed-interval chapter fallback (default 5)")
    ap.add_argument("--chapters-json",
                    help="path to a JSON list of {time, title} chapter boundaries to render verbatim")
    ap.add_argument("--session-title", help="front-matter session_title (else the video title)")
    ap.add_argument("--speaker", help="front-matter speaker (cannot be derived; pass it in)")
    ap.add_argument("--event", help="front-matter event (cannot be derived; pass it in)")
    args = ap.parse_args()

    try:
        video_id = extract_video_id(args.source)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 3

    langs = [x.strip() for x in args.lang.split(",") if x.strip()]
    url = f"https://www.youtube.com/watch?v={video_id}"

    # whether any structured output was requested
    want_chapters = args.chapters or bool(args.chapters_json)
    want_front_matter = (
        args.front_matter or want_chapters
        or any([args.session_title, args.speaker, args.event])
    )

    try:
        result = fetch_captions(video_id, langs)
        if result is not None:
            cues, lang_used = result
            source = f"captions:{lang_used}"
        else:
            if not args.fallback:
                print("NO_CAPTIONS", file=sys.stderr)
                return 2
            cues = whisper_fallback(video_id, args.whisper_model, args.keep_audio)
            lang_used = args.lang.split(",")[0]
            source = f"whisper:{args.whisper_model}"
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    # ----- resolve chapters
    chapters = None
    if args.chapters_json:
        try:
            chapters = load_chapters_json(args.chapters_json)
        except Exception as e:
            print(f"ERROR: could not read --chapters-json: {e}", file=sys.stderr)
            return 3
    elif args.chapters:
        info = fetch_ytdlp_info(video_id)
        chapters = native_chapters(info) or interval_chapters(
            total_duration(cues), args.chapter_interval
        )

    # ----- front matter
    front = ""
    if want_front_matter:
        meta = fetch_oembed(video_id)
        session_title = args.session_title or meta.get("title")
        channel = meta.get("author_name")
        fields = [
            ("url", url),
            ("video_id", video_id),
            ("session_title", session_title),
            ("speaker", args.speaker),
            ("event", args.event),
            ("channel", channel),
            ("duration", duration_str(total_duration(cues))),
            ("language", lang_used),
            ("source", source),
            ("extracted", _dt.date.today().isoformat()),
        ]
        front = build_front_matter(fields, chapters)

    # ----- body
    timestamps = args.timestamps or want_chapters
    body = render_body(cues, chapters, args.paragraph, timestamps)

    text = f"{front}\n\n{body}\n" if front else body
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)

    words = sum(len(c["text"].split()) for c in cues)
    extras = []
    if want_front_matter:
        extras.append("front-matter")
    if chapters:
        extras.append(f"{len(chapters)} chapters")
    if timestamps:
        extras.append("timestamps")
    suffix = f", {', '.join(extras)}" if extras else ""
    print(f"OK: wrote {args.out} ({words} words, video {video_id}, via {source}{suffix})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
