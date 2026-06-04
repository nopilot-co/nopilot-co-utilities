#!/usr/bin/env python3
# Part of the nopilot-co-utilities Claude Code plugin (utilities:theme-propose).
# STUB — scaffolding only. Invoked by skills/theme-propose/SKILL.md via
# $CLAUDE_PLUGIN_ROOT/scripts/propose.py. Also runnable standalone.
"""Non-destructive theme-framework proposal + governing theme manifest.

This NEVER edits source files. It has three modes:

  1. context (default)  — build `theme-context.json`: a compact digest of every
     enriched/summarised source (n, title, author, created, position, summary)
     for the model to reason over when proposing a theme framework. Prints the
     proposal JSON schema.

  2. --proposal-json FILE — materialise a model-produced proposal: writes
     `theme-proposal.json` (machine) + `theme-proposal.md` (human-editable) so
     the user can review/agree theme priorities and the editorial approach.

  3. --adopt FILE — freeze an agreed proposal into `theme-manifest.json`: the
     governing manifest (editorial, categorisation_guidance, ordered priorities,
     and per-theme editorial_approach + inclusion_criteria) that all subsequent
     theme-cluster / theme-entity runs read for guidance.

Proposal JSON schema (model-produced, also the shape of an edited proposal):
  {
    "icp": "who the evidence base serves — sourced from the messaging house (ICP)",
    "icp_source": "path/ref to the canonical ICP (e.g. messaging/people.md)",
    "editorial": "overall angle / purpose / audience for the evidence base",
    "categorisation_guidance": "how sources should be assigned to themes",
    "themes": [
      { "id": "agentic-gtm", "label": "Agentic GTM", "description": "...",
        "rationale": "why this is a coherent discussion thread",
        "priority": 1,
        "so_what": "why this theme matters to the ICP (the 'so what' angle)",
        "editorial_approach": "angle/what to foreground in this theme's dossier",
        "inclusion_criteria": "what qualifies a source for this theme",
        "candidate_members": ["<n|file>", ...] }
    ],
    "unthemed": ["<n>", ...]
  }

The `icp` + per-theme `so_what` are project judgment, NOT plugin logic — they are
carried as data, sourced from the messaging house (e.g. messaging/people.md), and
frozen into theme-manifest.json so every downstream run inherits them.

Usage:
  propose.py --batch DIR [--proposal-json FILE] [--adopt FILE]
             [--manifest theme-manifest.json] [--quiet]

Exit codes: 0 ok · 2 bad invocation (missing batch/manifest) · 3 error
"""
import argparse
import json
import os
import sys


def load_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        sys.stderr.write("error: PyYAML not installed — run install.sh or `pip install pyyaml`\n")
        sys.exit(3)


def read_front_matter(path):
    yaml = load_yaml()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            t = fh.read()
    except OSError:
        return {}
    if t.startswith("---\n"):
        parts = t.split("---\n", 2)
        if len(parts) == 3:
            return yaml.safe_load(parts[1]) or {}
    return {}


def build_digest(batch, manifest):
    """Compact, read-only digest of every enriched source for the model."""
    sources = []
    for e in manifest["sources"]:
        fm = read_front_matter(os.path.join(batch, e["file"]))
        if fm.get("enrich_status") != "enriched":
            continue
        sources.append({
            "n": e.get("n"),
            "file": e["file"],
            "title": fm.get("title"),
            "author": fm.get("author"),
            "created": fm.get("created"),
            "source_domain": fm.get("source_domain"),
            "position": fm.get("position"),
            "summary": fm.get("summary"),
        })
    return sources


SCHEMA_HINT = (
    'proposal JSON schema:\n'
    '  {"icp":"who this serves (from messaging/people.md)","icp_source":"...",\n'
    '   "editorial":"...","categorisation_guidance":"...",\n'
    '   "themes":[{"id","label","description","rationale","priority",\n'
    '              "so_what","editorial_approach","inclusion_criteria","candidate_members":[]}],\n'
    '   "unthemed":[]}'
)


def render_proposal_md(prop):
    lines = ["# Theme framework — proposal", "",
             "_Review and edit, then adopt to freeze `theme-manifest.json`._", "",
             "## ICP — who this serves (the 'so what' anchor)", "",
             prop.get("icp") or "_TODO: ICP, sourced from messaging/people.md_", "",
             "## Editorial approach (overall)", "", prop.get("editorial") or "_TODO_", "",
             "## Categorisation guidance", "", prop.get("categorisation_guidance") or "_TODO_", "",
             "## Proposed themes (by priority)", ""]
    themes = sorted(prop.get("themes", []), key=lambda t: t.get("priority", 999))
    for t in themes:
        cm = t.get("candidate_members", []) or []
        lines += [
            f"### {t.get('priority', '?')}. {t.get('label')}  `{t.get('id')}`",
            "",
            f"- **Description:** {t.get('description', '')}",
            f"- **So what (why it matters to the ICP):** {t.get('so_what', '')}",
            f"- **Why a coherent thread:** {t.get('rationale', '')}",
            f"- **Editorial approach:** {t.get('editorial_approach', '')}",
            f"- **Inclusion criteria:** {t.get('inclusion_criteria', '')}",
            f"- **Candidate sources ({len(cm)}):** {', '.join(str(x) for x in cm)}",
            "",
        ]
    unth = prop.get("unthemed", []) or []
    if unth:
        lines += [f"## Unthemed ({len(unth)})", "", ", ".join(str(x) for x in unth), ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Propose a theme framework + freeze a theme manifest (STUB, non-destructive).")
    ap.add_argument("--batch", required=True)
    ap.add_argument("--manifest-name", default="sources.json", help="batch manifest filename")
    ap.add_argument("--proposal-json", help="model-produced proposal to materialise (schema in docstring)")
    ap.add_argument("--adopt", help="an agreed proposal/manifest JSON to freeze into theme-manifest.json")
    ap.add_argument("--manifest", default="theme-manifest.json", help="output manifest filename within --batch")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    mpath = os.path.join(a.batch, a.manifest_name)
    if not os.path.isfile(mpath):
        sys.stderr.write(f"error: batch manifest not found: {mpath}\n")
        return 2
    with open(mpath, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    # --- mode 3: adopt -> freeze governing manifest (non-destructive)
    if a.adopt:
        with open(a.adopt, "r", encoding="utf-8") as fh:
            prop = json.load(fh)
        themes = sorted(prop.get("themes", []), key=lambda t: t.get("priority", 999))
        out = {
            "version": 1,
            "icp": prop.get("icp"),
            "icp_source": prop.get("icp_source"),
            "editorial": prop.get("editorial"),
            "categorisation_guidance": prop.get("categorisation_guidance"),
            "priorities": [t.get("id") for t in themes],
            "themes": [
                {k: t.get(k) for k in ("id", "label", "description", "priority",
                                       "so_what", "editorial_approach", "inclusion_criteria")}
                for t in themes
            ],
        }
        out_path = os.path.join(a.batch, a.manifest)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"adopted -> {out_path}  ({len(out['themes'])} themes; governs theme-cluster/theme-entity)")
        return 0

    # --- mode 2: materialise a model-produced proposal (non-destructive)
    if a.proposal_json:
        with open(a.proposal_json, "r", encoding="utf-8") as fh:
            prop = json.load(fh)
        pj = os.path.join(a.batch, "theme-proposal.json")
        pm = os.path.join(a.batch, "theme-proposal.md")
        with open(pj, "w", encoding="utf-8") as fh:
            json.dump(prop, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        with open(pm, "w", encoding="utf-8") as fh:
            fh.write(render_proposal_md(prop) + "\n")
        n = len(prop.get("themes", []))
        print(f"proposal materialised: {pj} + {pm}  ({n} themes)")
        print("review/edit theme-proposal.json, then: theme-propose --batch <dir> --adopt theme-proposal.json")
        return 0

    # --- mode 1: build the read-only context digest for the model
    digest = build_digest(a.batch, manifest)
    ctx_path = os.path.join(a.batch, "theme-context.json")
    with open(ctx_path, "w", encoding="utf-8") as fh:
        json.dump({"count": len(digest), "sources": digest}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"theme-context.json written: {len(digest)} summarised source(s) -> {ctx_path}")
    print("Non-destructive scan. Next: the model proposes a framework from this digest, then")
    print("  theme-propose --batch <dir> --proposal-json proposal.json   (materialise for review)")
    print(SCHEMA_HINT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
