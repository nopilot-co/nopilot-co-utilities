#!/usr/bin/env python3
"""Validate the nopilot-co-utilities marketplace structure.

Mechanically enforces the conventions documented in README.md (§ Adding a new
utility) and CLAUDE.md. Exits non-zero with a list of problems if anything is off.

Run: python3 .github/scripts/validate_marketplace.py
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Root entries that are NOT utility plugins (infra / docs / metadata).
ALLOWED_ROOT = {
    ".git", ".github", ".claude-plugin", ".gitignore", ".dual-graph", ".claude",
    "LICENSE", "README.md", "CLAUDE.md", "CONTEXT.md", "install.sh",
}

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        err(f"missing file: {path.relative_to(ROOT)}")
    except json.JSONDecodeError as e:
        err(f"invalid JSON in {path.relative_to(ROOT)}: {e}")
    return None


def root_plugins_array() -> set[str]:
    """Parse the PLUGINS=(...) array from the root install.sh."""
    text = (ROOT / "install.sh").read_text()
    m = re.search(r"PLUGINS=\(([^)]*)\)", text)
    if not m:
        err("root install.sh: could not find a PLUGINS=(...) array")
        return set()
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def has_shebang(path: Path) -> bool:
    try:
        first = path.read_text().splitlines()[0]
    except (IndexError, FileNotFoundError):
        return False
    return first.startswith("#!") and "python" in first


def validate_plugin(name: str, version: str, source: str, installed: set[str]) -> None:
    where = f"plugin '{name}'"

    if source != f"./{name}":
        err(f"{where}: marketplace source is '{source}', expected './{name}'")

    pdir = ROOT / name
    if not pdir.is_dir():
        err(f"{where}: directory '{name}/' does not exist")
        return

    # Manifest
    manifest = load_json(pdir / ".claude-plugin" / "plugin.json")
    if manifest is not None:
        if manifest.get("name") != name:
            err(f"{where}: plugin.json name '{manifest.get('name')}' != dir/catalog name '{name}'")
        if manifest.get("version") != version:
            err(f"{where}: version mismatch — plugin.json {manifest.get('version')!r} "
                f"vs marketplace.json {version!r} (bump BOTH to the same value)")

    # Required files
    if not (pdir / "requirements.txt").is_file():
        err(f"{where}: missing requirements.txt")

    skill = pdir / "skills" / name / "SKILL.md"
    if not skill.is_file():
        err(f"{where}: missing skills/{name}/SKILL.md")

    inst = pdir / "install.sh"
    if not inst.is_file():
        err(f"{where}: missing install.sh")
    elif not os.access(inst, os.X_OK):
        err(f"{where}: install.sh is not executable (chmod +x)")

    scripts = pdir / "scripts"
    pys = list(scripts.glob("*.py")) if scripts.is_dir() else []
    if not pys:
        err(f"{where}: no standalone script under scripts/*.py")
    for py in pys:
        if not has_shebang(py):
            err(f"{where}: {py.relative_to(ROOT)} lacks a '#!/usr/bin/env python3' shebang")
        if not os.access(py, os.X_OK):
            err(f"{where}: {py.relative_to(ROOT)} is not executable (chmod +x)")

    if name not in installed:
        err(f"{where}: not listed in the root install.sh PLUGINS=(...) array")


def main() -> int:
    mp = load_json(ROOT / ".claude-plugin" / "marketplace.json")
    if mp is None:
        print("FAIL: cannot read marketplace.json", file=sys.stderr)
        return 1

    if "plugins" not in mp or not mp["plugins"]:
        err("marketplace.json: no plugins listed")

    installed = root_plugins_array()
    catalog_names = set()

    for entry in mp.get("plugins", []):
        for field in ("name", "source", "version"):
            if field not in entry:
                err(f"marketplace entry {entry!r}: missing '{field}'")
        name = entry.get("name")
        if name:
            catalog_names.add(name)
            validate_plugin(name, entry.get("version"), entry.get("source", ""), installed)

    # No utility code at repo root / no stray plugin dirs outside the catalog.
    if (ROOT / "plugins").exists():
        err("found a 'plugins/' wrapper dir — utilities must be top-level dirs, not nested")
    for child in ROOT.iterdir():
        if child.name in ALLOWED_ROOT or not child.is_dir():
            continue
        if (child / ".claude-plugin" / "plugin.json").is_file() and child.name not in catalog_names:
            err(f"directory '{child.name}/' looks like a plugin but is not in marketplace.json")

    # PLUGINS array should not reference plugins that aren't in the catalog.
    for extra in installed - catalog_names:
        err(f"root install.sh PLUGINS lists '{extra}' which is not in marketplace.json")

    if errors:
        print(f"✗ marketplace validation failed ({len(errors)} problem(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"✓ marketplace valid: {len(catalog_names)} plugin(s) — {', '.join(sorted(catalog_names))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
