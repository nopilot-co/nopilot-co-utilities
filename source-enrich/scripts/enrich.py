#!/usr/bin/env python3
# Part of the nopilot-co-utilities Claude Code plugin (utilities:source-enrich).
# Invoked by skills/source-enrich/SKILL.md via $CLAUDE_PLUGIN_ROOT/scripts/enrich.py.
# Also runnable standalone: python3 scripts/enrich.py --batch path/to/sources
"""Enrich a notion-sources batch in place.

For each source in a `notion-sources` batch (a directory of `NNNN-<slug>.md`
files + a `sources.json` manifest) this:
  1. fetches the source page (Python `trafilatura` engine; YouTube reuses the
     youtube-transcript CLI; or ingests pre-fetched content via --html-file /
     --md-file for sources escalated through a logged-in browser / Firecrawl);
  2. fully populates the YAML front matter (real title, author, published,
     source_name, precis/description, word_count, lead_image, …);
  3. replaces the stub body with the extracted article (Markdown);
  4. downloads attached assets — inline images + linked documents — into
     assets/<slug>/, size-capped and hash-deduped, rewriting body image refs;
  5. appends a `## Appendix — Assets` section listing them.

It records `enrich_status` (enriched | partial | blocked | error) in the front
matter WITHOUT overwriting the Notion `status` field, and is resumable (skips
already-enriched files unless --reenrich). Auth-walled / JS sources that the
Python engine can't read are flagged `blocked` (no garbage) for escalation —
see SKILL.md.

Politeness: a realistic browser User-Agent, an inter-request --delay, request
timeouts, and optional --respect-robots. LinkedIn/X content is only ever read
from HTML you supply via --html-file (e.g. from your own logged-in browser);
this tool never bypasses authentication.

Usage:
    enrich.py --batch DIR [--only N|slug|id ...] [--limit N] [--reenrich]
              [--html-file PATH | --md-file PATH]   # with a single --only
              [--assets images,docs|images|none] [--max-asset-mb 25]
              [--delay 1.0] [--timeout 30] [--respect-robots] [--quiet]

Exit codes:
    0  ran (per-source failures are recorded, not fatal)
    2  bad invocation (batch/manifest missing, or --html-file without one --only)
    3  unexpected error
"""
import argparse
import datetime as _dt
import hashlib
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from urllib import robotparser

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
IMG_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "avif"}
DOC_EXT = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "tsv",
           "zip", "rtf", "odt", "ods", "odp", "epub", "key", "pages", "numbers",
           "txt", "json", "md"}
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
# image URLs that are page chrome, not article content — avatars, UI sprites,
# emoji/icons, tracking pixels, and (for social posts) commenter/reactor pics
ASSET_DENY = re.compile(
    r"(profile-display|/aero-v1/sc/h/|static\.licdn\.com/sc/|comment-image|"
    r"company-logo|/sprite|/emoji|emojicategory|/icons?/|favicon|"
    r"\b1x1\b|/pixel|/beacon|tracking|spacer|ghost-)", re.I)


def build_ssl_context():
    try:
        import truststore  # noqa: F401
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = build_ssl_context()


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------- front matter io
def load_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        sys.stderr.write("error: PyYAML not installed — run this utility's install.sh "
                         "or `pip install pyyaml`\n")
        sys.exit(3)


def read_md(path):
    """Return (front_matter_dict, body_str)."""
    yaml = load_yaml()
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) == 3:
            fm = yaml.safe_load(parts[1]) or {}
            return fm, parts[2].lstrip("\n")
    return {}, text


def write_md(path, fm, body):
    yaml = load_yaml()
    front = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=4096).rstrip("\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\n" + front + "\n---\n\n" + body.strip() + "\n")


# ----------------------------------------------------------------- http fetching
def http_get(url, timeout, referer=None, max_bytes=None):
    """GET a URL with a browser UA. Returns (bytes, content_type, status)."""
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        ctype = resp.headers.get("Content-Type", "")
        clen = resp.headers.get("Content-Length")
        if max_bytes and clen and clen.isdigit() and int(clen) > max_bytes:
            raise ValueError(f"too-large ({int(clen)} bytes)")
        cap = (max_bytes + 1) if max_bytes else None
        data = resp.read(cap) if cap else resp.read()
        if max_bytes and len(data) > max_bytes:
            raise ValueError("too-large")
        return data, ctype, resp.status


def fetch_html(url, timeout):
    """Fetch a page's HTML as text. Returns (html_or_None, status_or_reason)."""
    try:
        data, ctype, status = http_get(url, timeout, max_bytes=8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except Exception as e:
        return None, str(e)[:80]
    if "html" not in ctype and not data.lstrip()[:1] == b"<":
        return None, f"not-html ({ctype})"
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    try:
        return data.decode(charset, errors="replace"), status
    except LookupError:
        return data.decode("utf-8", errors="replace"), status


def extract_pdf(data):
    """Extract full text from PDF bytes via pypdf. Returns markdown-ish text or None."""
    try:
        import io
        import pypdf
    except ImportError:
        sys.stderr.write("error: pypdf not installed — run install.sh or `pip install pypdf`\n")
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            t = (page.extract_text() or "").strip()
            if t:
                parts.append(t)
        return "\n\n".join(parts).strip() or None
    except Exception:
        return None


def fetch_content(url, timeout):
    """Fetch a source and classify it. Returns (kind, payload, status):
    kind 'html' -> payload is HTML text; 'pdf'/'text' -> payload is extracted
    full text (Markdown-ish); 'none' -> payload None (blocked)."""
    try:
        data, ctype, status = http_get(url, timeout, max_bytes=25 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return "none", None, f"http {e.code}"
    except Exception as e:
        return "none", None, str(e)[:80]
    ctype_l = (ctype or "").lower()
    bare = url.lower().split("?")[0]
    if "application/pdf" in ctype_l or data[:5].lstrip().startswith(b"%PDF") or bare.endswith(".pdf"):
        text = extract_pdf(data)
        return ("pdf", text, status) if text else ("none", None, "pdf: no extractable text")
    if "html" in ctype_l or data.lstrip()[:1] == b"<":
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype_l)
        if m:
            charset = m.group(1)
        try:
            return "html", data.decode(charset, errors="replace"), status
        except LookupError:
            return "html", data.decode("utf-8", errors="replace"), status
    if ctype_l.startswith("text/") or bare.rsplit(".", 1)[-1] in ("txt", "md", "markdown", "csv"):
        return "text", data.decode("utf-8", errors="replace"), status
    return "none", None, f"unsupported type ({ctype_l or 'unknown'})"


def _route_content(kind, payload, url):
    """Map a fetch_content() result to (html, md, extractor) for process()."""
    if kind == "html":
        return payload, None, "trafilatura"
    if kind == "pdf":
        return None, payload, "pdf"
    if kind == "text":
        return None, payload, "text"
    return None, None, "trafilatura"  # none / blocked


# ----------------------------------------------------------------- html scanning
class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.imgs, self.links = [], []
        self.og_image = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "img":
            for k in ("src", "data-src", "data-delayed-url", "data-original", "data-srcset"):
                if d.get(k):
                    self.imgs.append(d[k].split(",")[0].strip().split(" ")[0])
                    break
            if d.get("srcset"):
                self.imgs.append(d["srcset"].split(",")[0].strip().split(" ")[0])
        elif tag == "source" and d.get("srcset"):
            self.imgs.append(d["srcset"].split(",")[0].strip().split(" ")[0])
        elif tag == "a" and d.get("href"):
            self.links.append(d["href"])
        elif tag == "meta" and d.get("property") == "og:image" and d.get("content"):
            self.og_image = d["content"]


def _ext_of(url):
    path = urllib.parse.urlparse(url).path
    base = os.path.basename(path)
    if "." in base:
        return base.rsplit(".", 1)[1].lower().split("?")[0]
    return ""


def collect_assets(html, body_md, base_url, mode):
    """Return (image_urls, doc_urls) absolute + de-duped, honouring `mode`."""
    images, docs = [], []
    seen = set()

    def add(lst, u):
        if not u or u.startswith("data:"):
            return
        absu = urllib.parse.urljoin(base_url, u)
        if absu not in seen:
            seen.add(absu)
            lst.append(absu)

    if html:
        c = _Collector()
        try:
            c.feed(html)
        except Exception:
            pass
        if c.og_image and mode != "none":
            add(images, c.og_image)
        if mode != "none":
            for u in c.imgs:
                add(images, u)
        for u in c.links:
            if _ext_of(u) in DOC_EXT:
                add(docs, u)
    # images referenced directly in the extracted markdown
    if mode != "none":
        for u in IMG_RE.findall(body_md or ""):
            add(images, u)
    # drop page-chrome images (avatars, sprites, icons, tracking, comment pics)
    images = [u for u in images if not ASSET_DENY.search(u)]
    if mode == "none":
        images = []
    if mode == "images":
        docs = []
    return images, docs


# --------------------------------------------------------------------- downloads
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(url, fallback_ext=""):
    base = os.path.basename(urllib.parse.urlparse(url).path) or "asset"
    base = base.split("?")[0]
    base = _SAFE.sub("-", base).strip("-.") or "asset"
    if "." not in base and fallback_ext:
        base = f"{base}.{fallback_ext}"
    return base[:80]


def download_assets(urls, dest_dir, cap_bytes, timeout, referer, delay, hashes, log):
    """Download a list of asset URLs. Returns (saved, catalogued).
    saved = [{url, path, bytes}]; catalogued = [{url, reason}]."""
    saved, catalogued = [], []
    for i, url in enumerate(urls, 1):
        try:
            data, ctype, _ = http_get(url, timeout, referer=referer, max_bytes=cap_bytes)
        except ValueError as e:
            catalogued.append({"url": url, "reason": str(e)})
            continue
        except Exception as e:
            catalogued.append({"url": url, "reason": str(e)[:60]})
            continue
        if not data:
            catalogued.append({"url": url, "reason": "empty"})
            continue
        if "text/html" in ctype.lower():
            # a link that resolves to a web page, not a downloadable file
            # (e.g. a GitHub /blob/ viewer) — catalogue, don't save
            catalogued.append({"url": url, "reason": "not a file (html page)"})
            continue
        h = hashlib.sha256(data).hexdigest()
        if h in hashes:
            saved.append({"url": url, "path": hashes[h], "bytes": len(data), "dup": True})
            continue
        ext = _ext_of(url) or _ctype_ext(ctype)
        name = f"{i:02d}-{_safe_name(url, ext)}"
        os.makedirs(dest_dir, exist_ok=True)
        full = os.path.join(dest_dir, name)
        with open(full, "wb") as fh:
            fh.write(data)
        rel = os.path.relpath(full, os.path.dirname(dest_dir))
        hashes[h] = rel
        saved.append({"url": url, "path": rel, "bytes": len(data)})
        if delay:
            time.sleep(delay)
    return saved, catalogued


def _ctype_ext(ctype):
    m = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
         "image/webp": "webp", "image/svg+xml": "svg", "application/pdf": "pdf",
         "application/zip": "zip", "text/csv": "csv"}
    return m.get(ctype.split(";")[0].strip(), "")


def _fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ----------------------------------------------------------------------- extract
def extract_article(html, url):
    """Run trafilatura. Returns (markdown_or_None, metadata_dict)."""
    try:
        import trafilatura
    except ImportError:
        sys.stderr.write("error: trafilatura not installed — run install.sh or "
                         "`pip install trafilatura`\n")
        sys.exit(3)
    md = trafilatura.extract(
        html, url=url, output_format="markdown", include_images=True,
        include_links=True, favor_recall=True, with_metadata=False)
    meta = {}
    try:
        doc = trafilatura.extract_metadata(html, default_url=url)
        if doc is not None:
            meta = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc.__dict__)
    except Exception:
        pass
    return md, meta


def looks_blocked(md, html):
    if md and len(md.split()) >= 40:
        return False
    if html:
        head = html[:4000].lower()
        for sig in ("sign in", "log in", "join now", "authwall", "please enable javascript",
                    "captcha", "verify you are human", "create your free account"):
            if sig in head:
                return True
    return not md or len(md.split()) < 40


# ------------------------------------------------------------------ youtube path
def youtube_transcript(url, timeout):
    """Reuse the sibling youtube-transcript CLI if present. Returns md or None."""
    import shutil
    import subprocess
    import tempfile
    exe = shutil.which("yt-transcript")
    if not exe:
        return None
    out = os.path.join(tempfile.mkdtemp(), "t.md")
    try:
        subprocess.run([exe, url, "--out", out, "--paragraph"],
                       timeout=max(timeout, 120), capture_output=True, check=True)
        with open(out, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return None


def is_youtube(url):
    h = urllib.parse.urlparse(url).netloc.lower()
    return h.endswith("youtube.com") or h == "youtu.be" or h.endswith(".youtu.be")


# ---------------------------------------------------------------------- assembly
def clean_author(a):
    if not a:
        return None
    if isinstance(a, list):
        a = "; ".join(str(x) for x in a if x)
    a = re.sub(r"^\s*by\s+", "", str(a).strip(), flags=re.I)
    return a or None


def build_appendix(saved_imgs, saved_docs, catalogued):
    lines = ["## Appendix — Assets", ""]
    n_img, n_doc = len(saved_imgs), len(saved_docs)
    lines.append(f"_{n_img} image(s), {n_doc} download(s) saved; "
                 f"{len(catalogued)} catalogued._")
    if saved_imgs:
        lines += ["", "### Images", ""]
        for i, a in enumerate(saved_imgs, 1):
            lines.append(f"{i}. ![]({a['path']}) — source: {a['url']} ({_fmt_size(a['bytes'])})")
    if saved_docs:
        lines += ["", "### Downloads", ""]
        for i, a in enumerate(saved_docs, 1):
            name = os.path.basename(a["path"])
            lines.append(f"{i}. [{name}]({a['path']}) — source: {a['url']} ({_fmt_size(a['bytes'])})")
    if catalogued:
        lines += ["", "### Catalogued (not downloaded)", ""]
        for a in catalogued:
            lines.append(f"- {a['url']} — {a['reason']}")
    return "\n".join(lines)


def enrich_front_matter(fm, meta, url, status, extractor, http_status, word_count, n_assets):
    """Fill nulls + add enrichment fields, preserving everything else."""
    def empty(v):
        return v is None or v == "" or v == [] or (isinstance(v, str) and v.startswith("http"))

    title = meta.get("title")
    if title and empty(fm.get("title")):
        fm["title"] = title
    author = clean_author(meta.get("author"))
    if author and not fm.get("author"):
        fm["author"] = author
    if empty(fm.get("precis")) and meta.get("description"):
        fm["precis"] = meta["description"]
    # enrichment block (appended/overwritten, never touches Notion `status`)
    fm["enriched"] = status == "enriched"
    fm["enrich_status"] = status
    fm["enriched_at"] = now_iso()
    fm["extractor"] = extractor
    fm["http_status"] = http_status
    if meta.get("sitename") or meta.get("hostname"):
        fm["source_name"] = meta.get("sitename") or meta.get("hostname")
    if meta.get("date"):
        fm["published"] = str(meta["date"])[:10]
    if meta.get("image"):
        fm["lead_image"] = meta["image"]
    if word_count:
        fm["word_count"] = word_count
    fm["assets_count"] = n_assets
    return fm


# ---------------------------------------------------------------------- per item
def process(entry, batch_dir, html_override, md_override, opts, hashes, log):
    path = os.path.join(batch_dir, entry["file"])
    if not os.path.exists(path):
        return {"file": entry["file"], "enrich_status": "error", "reason": "file missing"}
    fm, _body = read_md(path)
    url = fm.get("url") or entry.get("url")
    slug = os.path.splitext(entry["file"])[0]

    if fm.get("enriched") and not opts.reenrich:
        return {"file": entry["file"], "enrich_status": "skip"}

    extractor = "trafilatura"
    http_status = None
    html = html_override
    md = None
    meta = {}

    # 1. obtain content
    if md_override is not None:
        md, extractor, http_status = md_override, "md-file", "supplied"
    elif html is not None:
        extractor, http_status = "html-file", "supplied"
        md, meta = extract_article(html, url)
    elif is_youtube(url):
        md = youtube_transcript(url, opts.timeout)
        if md:
            extractor, http_status = "youtube-transcript", "ok"
        else:
            kind, payload, http_status = fetch_content(url, opts.timeout)
            html, md, extractor = _route_content(kind, payload, url)
            if kind == "html" and html:
                md, meta = extract_article(html, url)
    else:
        kind, payload, http_status = fetch_content(url, opts.timeout)
        html, md, extractor = _route_content(kind, payload, url)
        if kind == "html" and html:
            md, meta = extract_article(html, url)

    # 2. blocked?
    if extractor in ("md-file", "youtube-transcript", "pdf", "text"):
        status = "enriched" if md else "blocked"
    elif html is None:
        status = "blocked"          # fetch failed (network / 4xx / 999 / not-html)
    elif looks_blocked(md, html):
        status = "blocked"          # login wall / JS shell / too little content
    else:
        status = "enriched"

    if status == "blocked":
        enrich_front_matter(fm, meta, url, "blocked", extractor, http_status, 0, 0)
        write_md(path, fm, _body)  # keep existing stub body
        log(f"  · blocked: {entry['file']}  ({http_status})")
        return {"file": entry["file"], "enrich_status": "blocked", "url": url,
                "reason": http_status, "title": fm.get("title"), "author": fm.get("author")}

    # 3. assets
    saved_imgs = saved_docs = []
    catalogued = []
    if opts.assets != "none":
        imgs, docs = collect_assets(html, md, url, opts.assets)
        imgs, docs = imgs[:opts.max_assets], docs[:opts.max_assets]
        assets_dir = os.path.join(batch_dir, "assets", slug)
        # regenerate cleanly — clear any prior (possibly stale) assets for this slug
        import shutil
        shutil.rmtree(assets_dir, ignore_errors=True)
        cap = int(opts.max_asset_mb * 1024 * 1024)
        si, ci = download_assets(imgs, assets_dir, cap, opts.timeout, url, opts.delay, hashes, log)
        sd, cd = download_assets(docs, assets_dir, cap, opts.timeout, url, opts.delay, hashes, log)
        saved_imgs, saved_docs, catalogued = si, sd, ci + cd
        # rewrite body image refs that we downloaded to local paths
        urlmap = {a["url"]: a["path"] for a in saved_imgs}
        if md:
            for src, dst in urlmap.items():
                md = md.replace(src, dst)

    n_assets = len(saved_imgs) + len(saved_docs)
    # body extracted but every asset we found failed to download -> partial
    final = "enriched"
    if opts.assets != "none" and catalogued and n_assets == 0:
        final = "partial"

    # PDFs/plaintext carry no HTML metadata — derive a title from the first
    # substantial line so the front matter isn't left as the raw URL.
    if extractor in ("pdf", "text") and not meta.get("title") and md:
        first = next((ln.strip() for ln in md.splitlines() if len(ln.strip()) > 12), "")
        if first:
            meta["title"] = " ".join(first.split())[:200]

    # 4. assemble body
    word_count = len((md or "").split())
    body = (md or "").strip()
    if opts.assets != "none" and (saved_imgs or saved_docs or catalogued):
        body += "\n\n" + build_appendix(saved_imgs, saved_docs, catalogued)
    enrich_front_matter(fm, meta, url, final, extractor, http_status, word_count, n_assets)
    write_md(path, fm, body)
    log(f"  ✓ {entry['file']}  [{final}] {word_count}w, {n_assets} asset(s)")
    return {"file": entry["file"], "enrich_status": final, "url": url,
            "title": fm.get("title"), "author": fm.get("author"),
            "word_count": word_count, "assets": n_assets, "extractor": extractor}


# --------------------------------------------------------------------- robots
_ROBOTS = {}


def robots_ok(url, timeout):
    parts = urllib.parse.urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    rp = _ROBOTS.get(base)
    if rp is None:
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = False
        _ROBOTS[base] = rp
    if rp is False:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


# ------------------------------------------------------------------------- main
import json  # noqa: E402  (kept near use for clarity)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Enrich a notion-sources batch in place.")
    ap.add_argument("--batch", required=True, help="batch directory (holds sources.json + NNNN-*.md)")
    ap.add_argument("--manifest", default="sources.json", help="manifest filename within --batch")
    ap.add_argument("--only", action="append", default=[], help="enrich only these (n / slug / id); repeatable")
    ap.add_argument("--limit", type=int, default=0, help="stop after N processed (0 = all)")
    ap.add_argument("--reenrich", action="store_true", help="re-process already-enriched sources")
    ap.add_argument("--html-file", help="ingest this pre-fetched HTML (requires a single --only)")
    ap.add_argument("--md-file", help="ingest this pre-fetched Markdown body (requires a single --only)")
    ap.add_argument("--assets", default="images,docs",
                    choices=["images,docs", "images", "none"], help="what to download")
    ap.add_argument("--max-asset-mb", type=float, default=25.0, help="per-asset size cap (MB)")
    ap.add_argument("--max-assets", type=int, default=50, help="max images/docs per source")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between network requests")
    ap.add_argument("--timeout", type=int, default=30, help="per-request timeout (s)")
    ap.add_argument("--respect-robots", action="store_true", help="skip URLs disallowed by robots.txt")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    # collect_assets() modes: "both" (images+docs), "images", "none"
    args.assets = {"images,docs": "both", "images": "images", "none": "none"}[args.assets]

    batch = os.path.abspath(args.batch)
    manifest_path = os.path.join(batch, args.manifest)
    if not os.path.isfile(manifest_path):
        sys.stderr.write(f"error: manifest not found: {manifest_path}\n")
        return 2
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    if (args.html_file or args.md_file) and len(args.only) != 1:
        sys.stderr.write("error: --html-file/--md-file require exactly one --only target\n")
        return 2

    def info(m):
        if not args.quiet:
            print(m)

    # selection
    sel = set(str(x) for x in args.only)

    def matches(e):
        if not sel:
            return True
        slug = os.path.splitext(e["file"])[0]
        return (str(e.get("n")) in sel          # row number, e.g. 8
                or e.get("id") in sel            # Notion page id
                or slug in sel                   # full slug, e.g. 0008-github-...
                or e["file"] in sel              # filename
                or f"{e.get('n', 0):04d}" in sel)  # zero-padded number, e.g. 0008

    html_override = md_override = None
    if args.html_file:
        with open(args.html_file, "r", encoding="utf-8", errors="replace") as fh:
            html_override = fh.read()
    if args.md_file:
        with open(args.md_file, "r", encoding="utf-8", errors="replace") as fh:
            md_override = fh.read()

    hashes = {}
    results = []
    processed = 0
    by_file = {e["file"]: e for e in manifest["sources"]}

    try:
        for entry in manifest["sources"]:
            if args.limit and processed >= args.limit:
                break
            if not matches(entry):
                continue
            url = entry.get("url")
            if (args.respect_robots and url and html_override is None
                    and md_override is None and not robots_ok(url, args.timeout)):
                info(f"  · robots-disallowed: {entry['file']}")
                results.append({"file": entry["file"], "enrich_status": "blocked", "reason": "robots"})
                continue
            try:
                r = process(entry, batch, html_override, md_override, args, hashes, info)
            except Exception as e:
                r = {"file": entry["file"], "enrich_status": "error", "reason": str(e)[:120]}
                info(f"  ! error: {entry['file']}: {e}")
            results.append(r)
            if r.get("enrich_status") != "skip":
                processed += 1
                if args.delay and html_override is None and md_override is None:
                    time.sleep(args.delay)
            # update manifest entry
            e = by_file.get(r["file"])
            if e and r.get("enrich_status") not in ("skip",):
                e["enrich_status"] = r.get("enrich_status")
                e["enriched"] = r.get("enrich_status") == "enriched"
                if r.get("title"):
                    e["title"] = r["title"]
                if r.get("author"):
                    e["author"] = r["author"]
    except KeyboardInterrupt:
        info("\ninterrupted — writing manifest…")

    manifest["enriched_at"] = now_iso()
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # summary
    from collections import Counter
    counts = Counter(r.get("enrich_status") for r in results)
    print(f"\nprocessed {processed} source(s) in {batch}")
    for k in ("enriched", "partial", "blocked", "error", "skip"):
        if counts.get(k):
            print(f"  {k}: {counts[k]}")
    blocked = [r for r in results if r.get("enrich_status") == "blocked"]
    if blocked:
        print("  blocked (escalate via a logged-in browser / Firecrawl, then "
              "--only N --html-file <file>):")
        for r in blocked[:20]:
            print(f"    - {r['file']}  {r.get('url','')}")
        if len(blocked) > 20:
            print(f"    … and {len(blocked) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
