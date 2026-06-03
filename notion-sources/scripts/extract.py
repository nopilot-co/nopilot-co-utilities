#!/usr/bin/env python3
# Part of the nopilot-co-utilities Claude Code plugin (utilities:notion-sources).
# Invoked by skills/notion-sources/SKILL.md via $CLAUDE_PLUGIN_ROOT/scripts/extract.py.
# Also runnable standalone: python3 scripts/extract.py --out sources/
"""Extract a Notion database into a batch of per-source Markdown files + a manifest.

For every row in a Notion database this writes one `NNNN-<slug>.md` file with a
YAML front-matter block, and maintains a manifest (`sources.json` + a human
`index.md`) listing every source in the batch — ready for a downstream batch
utility to iterate.

Schema-agnostic by design. It does NOT assume column names. For each row it:
  • finds a source URL by scanning every property (url-typed first, then any
    URL embedded in the title or other text) — URL detection is primary;
  • maps normalised metadata (status, category, tags, precis, favorite,
    archived, created, topic/content relations) by property *type* and a fuzzy
    *name* match, leaving fields null when a database doesn't carry them;
  • preserves every original column verbatim under a `properties:` block so no
    data is lost across differently-structured databases;
  • derives `author` / `author_profile_url` from the source URL where the host
    allows it (LinkedIn, X/Twitter, YouTube, Medium, Substack, GitHub), else null.

Incremental: if the output dir already holds a manifest, a re-run APPENDS only
sources whose Notion page id (URL as secondary key) isn't already present;
existing files and numbering are preserved. `--fresh` rebuilds from scratch.

Auth (no SDK — plain HTTP). Token and database id come from flags or env:
  NOPILOT_NOTION_API_KEY            (also NOTION_API_KEY / NOTION_TOKEN)
  NOPILOT_NOTION_SOURCE_DATABASE_ID (also the misspelled …DATABASAE_ID,
                                     and NOTION_DATABASE_ID)
A `.env` file (cwd `./.env` by default, or --env-file) is loaded if present;
real environment variables take precedence and are never printed.

Usage:
    extract.py [--database ID] [--token KEY] [--out sources/] [--env-file .env]
               [--include-archived] [--status NAME]... [--category NAME]
               [--favorite] [--fresh] [--limit N] [--no-relations] [--quiet]

Exit codes:
    0  success
    2  auth/permission failure (missing/invalid token, or DB not shared with the
       integration) — nothing was written
    3  other error
"""
import argparse
import datetime as _dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
URL_RE = re.compile(r"https?://[^\s<>\")']+")


def _build_ssl_context():
    """A TLS context that actually trusts a CA on machines (e.g. macOS python.org
    builds) where the stdlib default can't find one. Order: system trust store via
    truststore → certifi bundle → stdlib default. Verification is always on."""
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


SSL_CTX = _build_ssl_context()


# --------------------------------------------------------------------------- env
def load_env_file(path):
    """Populate os.environ from a simple KEY=VALUE .env (without overriding)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


def first_env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return None


# ----------------------------------------------------------------- notion client
def api_request(method, path, token, body=None, retries=5):
    """Call the Notion API; return parsed JSON. Retries on 429/5xx with backoff."""
    url = NOTION_API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            payload = ""
            try:
                payload = e.read().decode("utf-8")
            except Exception:
                pass
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else (2 ** attempt)
                time.sleep(min(delay, 30))
                continue
            raise NotionError(e.code, payload)
        except urllib.error.URLError as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise NotionError(0, str(e.reason))
    if last_err:
        raise NotionError(0, str(last_err))


class NotionError(Exception):
    def __init__(self, code, payload):
        self.code = code
        self.payload = payload
        super().__init__(f"Notion API error {code}: {payload[:300]}")


def query_database(database_id, token, page_filter=None, page_size=100):
    """Yield every page object in a database, following pagination."""
    cursor = None
    while True:
        body = {"page_size": page_size}
        if cursor:
            body["start_cursor"] = cursor
        if page_filter:
            body["filter"] = page_filter
        res = api_request("POST", f"/databases/{database_id}/query", token, body)
        for page in res.get("results", []):
            yield page
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")


# ------------------------------------------------------------ property flattening
def _rich_text_plain(rt):
    return "".join(seg.get("plain_text", "") for seg in (rt or [])).strip()


def flatten_prop(prop, token, resolve_relations, cache):
    """Reduce any Notion property value to a JSON-friendly scalar/list/None."""
    t = prop.get("type")
    v = prop.get(t)
    if v is None:
        return None
    if t in ("title", "rich_text"):
        return _rich_text_plain(v) or None
    if t == "url":
        return v or None
    if t == "email":
        return v or None
    if t == "phone_number":
        return v or None
    if t == "number":
        return v
    if t == "checkbox":
        return bool(v)
    if t == "select":
        return v.get("name") if v else None
    if t == "status":
        return v.get("name") if v else None
    if t == "multi_select":
        return [o.get("name") for o in v] or []
    if t in ("created_time", "last_edited_time"):
        return v
    if t == "date":
        if not v:
            return None
        return v.get("end") and f"{v.get('start')}/{v.get('end')}" or v.get("start")
    if t in ("created_by", "last_edited_by"):
        return _person_name(v)
    if t == "people":
        return [_person_name(p) for p in v] or []
    if t == "files":
        out = []
        for f in v:
            ft = f.get("type")
            if ft == "external":
                out.append(f.get("external", {}).get("url"))
            elif ft == "file":
                out.append(f.get("file", {}).get("url"))
            else:
                out.append(f.get("name"))
        return [x for x in out if x] or []
    if t == "formula":
        ft = v.get("type")
        fv = v.get(ft)
        if ft == "date" and isinstance(fv, dict):
            return fv.get("start")
        return fv if fv != "" else None
    if t == "unique_id":
        num = v.get("number")
        pre = v.get("prefix")
        return f"{pre}-{num}" if pre else num
    if t == "rollup":
        rt = v.get("type")
        if rt == "array":
            return [flatten_prop({"type": e.get("type"), e.get("type"): e.get(e.get("type"))},
                                 token, False, cache) for e in v.get("array", [])]
        return v.get(rt)
    if t == "relation":
        ids = [r.get("id") for r in v if r.get("id")]
        if not ids:
            return []
        if resolve_relations:
            return [resolve_page_title(i, token, cache) for i in ids]
        return ids
    # Unknown / non-data types (button, verification, etc.)
    return None


def _person_name(p):
    if not p:
        return None
    if isinstance(p, list):
        return [_person_name(x) for x in p]
    return p.get("name") or p.get("id")


def resolve_page_title(page_id, token, cache):
    """Return a related page's title text (cached). Falls back to the id."""
    if page_id in cache:
        return cache[page_id]
    title = page_id
    try:
        page = api_request("GET", f"/pages/{page_id}", token)
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = _rich_text_plain(prop.get("title")) or page_id
                break
    except NotionError:
        pass
    cache[page_id] = title
    return title


# ------------------------------------------------------------------ url / author
def find_urls(value):
    """Recursively pull http(s) URLs out of any flattened value."""
    found = []
    if isinstance(value, str):
        found += [u.rstrip(".,);]'\"") for u in URL_RE.findall(value)]
    elif isinstance(value, list):
        for x in value:
            found += find_urls(x)
    return found


def pick_primary_url(props_meta, flat):
    """Choose the row's source URL. Priority: url-typed props, then title, then
    any other text/field. Returns (url, None) or (None, reason)."""
    # 1. explicit url-typed properties, in document order
    for name, t in props_meta:
        if t == "url" and flat.get(name):
            return flat[name], None
    # 2. a URL embedded in the title
    for name, t in props_meta:
        if t == "title":
            urls = find_urls(flat.get(name))
            if urls:
                return urls[0], None
    # 3. a URL anywhere else (rich_text, formula, files, …)
    for name, t in props_meta:
        urls = find_urls(flat.get(name))
        if urls:
            return urls[0], None
    return None, "no URL found in any column"


def domain_of(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def derive_author(url):
    """Best-effort (author, author_profile_url) from the source URL. (None, None)
    when the host doesn't expose it in the URL."""
    if not url:
        return None, None
    p = urllib.parse.urlparse(url)
    host = domain_of(url)
    segs = [s for s in p.path.split("/") if s]

    if host.endswith("linkedin.com"):
        if len(segs) >= 2 and segs[0] == "posts":
            handle = segs[1].split("_")[0]
            if handle:
                return handle, f"https://www.linkedin.com/in/{handle}"
        if len(segs) >= 2 and segs[0] == "in":
            return segs[1], f"https://www.linkedin.com/in/{segs[1]}"
        if len(segs) >= 2 and segs[0] == "company":
            return segs[1], f"https://www.linkedin.com/company/{segs[1]}"
    if host in ("twitter.com", "x.com") and segs:
        h = segs[0]
        if h.lower() not in ("i", "home", "search", "hashtag", "intent"):
            return h, f"https://x.com/{h}"
    if host.endswith("youtube.com") or host == "youtu.be":
        a = _youtube_author(url)
        if a:
            return a
    if host.endswith("medium.com") and segs and segs[0].startswith("@"):
        return segs[0], f"https://medium.com/{segs[0]}"
    if host.endswith(".substack.com"):
        sub = host.split(".")[0]
        return sub, f"https://{host}"
    if host == "github.com" and segs:
        return segs[0], f"https://github.com/{segs[0]}"
    return None, None


def _youtube_author(url):
    """Keyless oEmbed lookup for a YouTube video's channel."""
    try:
        oe = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(oe, timeout=10, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("author_name"), data.get("author_url")
    except Exception:
        return None


# ----------------------------------------------------------- metadata classifier
def _match(name, pattern):
    return re.search(pattern, name, re.I) is not None


def classify(props_meta, flat):
    """Map flattened properties to normalised fields by type + fuzzy name."""
    selects = [(n, t) for n, t in props_meta if t == "select"]
    multis = [(n, t) for n, t in props_meta if t == "multi_select"]
    checks = [(n, t) for n, t in props_meta if t == "checkbox"]
    rels = [(n, t) for n, t in props_meta if t == "relation"]
    texts = [(n, t) for n, t in props_meta if t == "rich_text"]
    dates = [(n, t) for n, t in props_meta if t in ("date", "created_time")]

    # status: a real status-typed prop wins; else a select named status/state
    status = None
    for n, t in props_meta:
        if t == "status" and flat.get(n):
            status = flat[n]
            break
    if status is None:
        for n, _ in selects:
            if _match(n, r"status|state|stage"):
                status = flat.get(n)
                break

    # tags: a multi_select named tag/label/keyword, else the first multi_select
    tags = []
    tag_name = next((n for n, _ in multis if _match(n, r"tag|label|keyword|topic")), None)
    if tag_name is None and multis:
        tag_name = multis[0][0]
    if tag_name:
        tags = flat.get(tag_name) or []

    # category: a select named categor/type/kind/section, else first unused select
    used_select = None
    category = None
    cat_name = next((n for n, _ in selects if _match(n, r"categor|type|kind|section|bucket|group")), None)
    if cat_name is None:
        cat_name = next((n for n, _ in selects if flat.get(n) != status or status is None), None)
    if cat_name:
        category = flat.get(cat_name)
        used_select = cat_name

    # precis: rich_text named summary/precis/description/notes/abstract/excerpt
    precis = ""
    precis_name = next((n for n, _ in texts
                        if _match(n, r"precis|summary|descr|notes?|abstract|excerpt|comment|caption")), None)
    if precis_name:
        precis = flat.get(precis_name) or ""

    # favorite: checkbox named fav/star/pin/like
    favorite = False
    fav_name = next((n for n, _ in checks if _match(n, r"fav|star|pin|like|bookmark")), None)
    if fav_name:
        favorite = bool(flat.get(fav_name))

    # archived: checkbox named archiv/trash (page-level handled by caller too)
    archived = False
    arch_name = next((n for n, _ in checks if _match(n, r"archiv|trash")), None)
    if arch_name:
        archived = bool(flat.get(arch_name))

    # created: created_time type, else a date named created/added, else None
    created = None
    ct = next((n for n, t in props_meta if t == "created_time"), None)
    if ct:
        created = flat.get(ct)
    else:
        cd = next((n for n, _ in dates if _match(n, r"creat|added|date|saved|captured")), None)
        if cd:
            created = flat.get(cd)
    if isinstance(created, str):
        created = created[:10]  # YYYY-MM-DD

    # relations → resolved title lists; surface topics/content, keep the rest
    topics, content, other_rels = [], [], {}
    for n, _ in rels:
        vals = flat.get(n) or []
        if _match(n, r"topic"):
            topics = vals
        elif _match(n, r"content|article|source|page|link"):
            content = vals
        elif vals:
            other_rels[n] = vals

    return {
        "status": status,
        "category": category,
        "tags": tags,
        "topics": topics,
        "content": content,
        "precis": precis or "",
        "favorite": favorite,
        "archived": archived,
        "created": created,
        "_relations_extra": other_rels,
        "_used": {n for n in (tag_name, used_select, precis_name, fav_name, arch_name, ct) if n},
    }


# ----------------------------------------------------------------- yaml / output
def yscalar(v):
    return json.dumps(v, ensure_ascii=False)


def dump_yaml(obj, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                if v:
                    lines.append(f"{pad}{k}:")
                    lines.append(dump_yaml(v, indent + 1))
                else:
                    lines.append(f"{pad}{k}: {{}}")
            elif isinstance(v, list):
                if v and all(not isinstance(x, (dict, list)) for x in v):
                    lines.append(f"{pad}{k}:")
                    for x in v:
                        lines.append(f"{pad}  - {yscalar(x)}")
                elif v:
                    lines.append(f"{pad}{k}: {yscalar(v)}")
                else:
                    lines.append(f"{pad}{k}: []")
            else:
                lines.append(f"{pad}{k}: {yscalar(v)}")
    return "\n".join(lines)


def slugify(title, url, author):
    text = "" if (title and re.match(r"https?://", title)) else (title or "")
    parts = []
    dom = domain_of(url)
    if dom:
        parts.append(dom.split(".")[0])
    if author:
        parts.append(author)
    if text:
        parts += re.findall(r"[A-Za-z0-9]+", text)[:6]
    elif url:
        parts += re.findall(r"[A-Za-z0-9]+", urllib.parse.urlparse(url).path)[:6]
    slug = "-".join(p.lower() for p in parts if p)
    slug = re.sub(r"-+", "-", slug).strip("-")[:60]
    return slug or "source"


def build_front_matter(fields):
    fm = {k: fields[k] for k in (
        "id", "url", "title", "author", "author_profile_url",
        "status", "category", "tags", "topics", "content",
        "precis", "favorite", "archived", "created",
        "source_domain", "notion_url", "extracted",
    )}
    if fields.get("relations"):
        fm["relations"] = fields["relations"]
    if fields.get("properties"):
        fm["properties"] = fields["properties"]
    return fm


def render_markdown(fields):
    fm = build_front_matter(fields)
    body_lines = [f"# {fields['title'] or fields['url']}", ""]
    body_lines.append(f"- **Source:** {fields['url']}")
    if fields.get("author"):
        prof = f" ({fields['author_profile_url']})" if fields.get("author_profile_url") else ""
        body_lines.append(f"- **Author:** {fields['author']}{prof}")
    if fields.get("status"):
        body_lines.append(f"- **Status:** {fields['status']}")
    if fields.get("precis"):
        body_lines += ["", fields["precis"]]
    body_lines += ["", "<!-- notion-sources stub — enrich below -->", ""]
    return "---\n" + dump_yaml(fm) + "\n---\n\n" + "\n".join(body_lines)


# ----------------------------------------------------------------------- manifest
def load_manifest(out_dir):
    path = os.path.join(out_dir, "sources.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {"database_id": None, "generated": None, "count": 0, "sources": []}


def write_manifest(out_dir, manifest):
    with open(os.path.join(out_dir, "sources.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def write_index(out_dir, manifest):
    rows = ["# Sources", "",
            f"Database: `{manifest.get('database_id')}` · {manifest['count']} source(s) · "
            f"generated {manifest.get('generated')}", "",
            "| # | Title | Author | Status | Domain | File | URL |",
            "|---|---|---|---|---|---|---|"]
    for s in manifest["sources"]:
        title = (s.get("title") or "").replace("|", "\\|")[:80]
        rows.append(
            f"| {s.get('n')} | {title} | {s.get('author') or '—'} | {s.get('status') or '—'} | "
            f"{s.get('domain') or '—'} | `{s.get('file')}` | {s.get('url')} |")
    with open(os.path.join(out_dir, "index.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")


# ---------------------------------------------------------------------------- run
def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract a Notion database into a batch of per-source Markdown files + manifest.")
    ap.add_argument("--database", help="Notion database id (else from env)")
    ap.add_argument("--token", help="Notion integration token (else from env)")
    ap.add_argument("--out", default="sources", help="output directory (default: sources/)")
    ap.add_argument("--env-file", help="path to a .env to load (default: ./.env if present)")
    ap.add_argument("--include-archived", action="store_true", help="include rows flagged archived")
    ap.add_argument("--status", action="append", default=[], help="only rows whose status matches (repeatable)")
    ap.add_argument("--category", help="only rows whose category matches")
    ap.add_argument("--favorite", action="store_true", help="only rows flagged favorite")
    ap.add_argument("--fresh", action="store_true", help="rebuild the batch from scratch (don't append)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    ap.add_argument("--no-relations", action="store_true", help="store relation ids, don't resolve to titles")
    ap.add_argument("--quiet", action="store_true", help="less output")
    args = ap.parse_args(argv)

    # --- env / credentials
    load_env_file(args.env_file or ".env")
    token = args.token or first_env("NOPILOT_NOTION_API_KEY", "NOTION_API_KEY", "NOTION_TOKEN")
    database = args.database or first_env(
        "NOPILOT_NOTION_SOURCE_DATABASE_ID", "NOPILOT_NOTION_SOURCE_DATABASAE_ID", "NOTION_DATABASE_ID")
    if not token:
        sys.stderr.write("error: no Notion token (set NOPILOT_NOTION_API_KEY or pass --token)\n")
        return 2
    if not database:
        sys.stderr.write("error: no database id (set NOPILOT_NOTION_SOURCE_DATABASE_ID or pass --database)\n")
        return 2

    def info(msg):
        if not args.quiet:
            print(msg)

    os.makedirs(args.out, exist_ok=True)
    manifest = {"database_id": None, "generated": None, "count": 0, "sources": []} if args.fresh \
        else load_manifest(args.out)
    seen_ids = {s["id"] for s in manifest["sources"]}
    seen_urls = {s.get("url") for s in manifest["sources"]}
    next_n = (max((s.get("n", 0) for s in manifest["sources"]), default=0)) + 1

    cache = {}  # related-page-id -> title
    added = skipped_nourl = skipped_dupe = skipped_filtered = 0

    try:
        pages = query_database(database, token)
        for page in pages:
            if args.limit and added >= args.limit:
                break
            props = page.get("properties", {})
            props_meta = [(n, p.get("type")) for n, p in props.items()]
            flat = {n: flatten_prop(p, token, not args.no_relations, cache) for n, p in props.items()}

            url, reason = pick_primary_url(props_meta, flat)
            meta = classify(props_meta, flat)
            page_archived = page.get("archived") or page.get("in_trash") or meta["archived"]

            # --- filters
            if not args.include_archived and page_archived:
                skipped_filtered += 1
                continue
            if args.status and (meta["status"] or "") not in args.status:
                skipped_filtered += 1
                continue
            if args.category and (meta["category"] or "") != args.category:
                skipped_filtered += 1
                continue
            if args.favorite and not meta["favorite"]:
                skipped_filtered += 1
                continue
            if not url:
                skipped_nourl += 1
                info(f"  · skip (no url): {page.get('id')}")
                continue

            pid = page.get("id")
            if pid in seen_ids or url in seen_urls:
                skipped_dupe += 1
                continue

            # --- title (first title-typed prop's text; fall back to the url)
            title = None
            for n, t in props_meta:
                if t == "title":
                    title = flat.get(n)
                    break

            author, author_url = derive_author(url)

            # --- preserve every column we didn't normalise, under properties:
            normalised_text = set()  # title-typed handled separately
            for n, t in props_meta:
                if t == "title":
                    normalised_text.add(n)
            preserved = {}
            for n, t in props_meta:
                if n in normalised_text:
                    continue
                val = flat.get(n)
                if val in (None, "", [], {}):
                    continue
                preserved[n] = val

            fields = {
                "id": pid,
                "url": url,
                "title": title or url,
                "author": author,
                "author_profile_url": author_url,
                "status": meta["status"],
                "category": meta["category"],
                "tags": meta["tags"],
                "topics": meta["topics"],
                "content": meta["content"],
                "precis": meta["precis"],
                "favorite": meta["favorite"],
                "archived": bool(page_archived),
                "created": meta["created"] or (page.get("created_time") or "")[:10] or None,
                "source_domain": domain_of(url),
                "notion_url": page.get("url"),
                "extracted": now_iso(),
                "relations": meta["_relations_extra"],
                "properties": preserved,
            }

            n = next_n
            next_n += 1
            fname = f"{n:04d}-{slugify(fields['title'], url, author)}.md"
            with open(os.path.join(args.out, fname), "w", encoding="utf-8") as fh:
                fh.write(render_markdown(fields))

            manifest["sources"].append({
                "n": n, "id": pid, "file": fname, "url": url,
                "title": fields["title"], "author": author,
                "author_profile_url": author_url, "status": meta["status"],
                "category": meta["category"], "tags": meta["tags"],
                "domain": fields["source_domain"], "created": fields["created"],
            })
            seen_ids.add(pid)
            seen_urls.add(url)
            added += 1
            info(f"  ✓ {fname}")

    except NotionError as e:
        if e.code in (401, 403, 404):
            sys.stderr.write(
                f"error: Notion auth/permission failure ({e.code}). Check the token and that the "
                f"database is shared with the integration.\n{e.payload[:300]}\n")
            return 2
        sys.stderr.write(f"error: {e}\n")
        return 3
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 3

    manifest["database_id"] = database
    manifest["generated"] = now_iso()
    manifest["count"] = len(manifest["sources"])
    manifest["sources"].sort(key=lambda s: s.get("n", 0))
    write_manifest(args.out, manifest)
    write_index(args.out, manifest)

    print(f"\n{added} new source(s) -> {args.out}/  (batch total: {manifest['count']})")
    if skipped_dupe:
        print(f"  {skipped_dupe} already in batch (skipped)")
    if skipped_filtered:
        print(f"  {skipped_filtered} filtered out")
    if skipped_nourl:
        print(f"  {skipped_nourl} had no detectable URL (skipped — see log above)")
    print(f"  manifest: {os.path.join(args.out, 'sources.json')} · index: {os.path.join(args.out, 'index.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
