#!/usr/bin/env python3
"""Prepare the files accompanying an interface output/description change.

Does not verify or stamp skills. Review the generated prose before committing.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = re.compile(r'^INTERFACE_CONTRACT_VERSION = "(\d+\.\d+\.\d+)"$', re.M)


def affected_skills(root: Path, sources: set[str]) -> dict[str, list[str]]:
    spec = importlib.util.spec_from_file_location(
        "release_freshness", root / "scripts/client/_check_freshness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    affected = {}
    for skill in sorted((root / "skills").glob("*/SKILL.md")):
        meta = module.parse_frontmatter(skill.read_text())
        cited = module.load_source_files(skill.parent, meta.get("source_files", []))
        matches = sorted(set(cited) & {"unitares/" + p for p in sources})
        if matches:
            affected[skill.parent.name] = matches
    return affected


def changed_sources(root: Path) -> set[str]:
    base = subprocess.check_output(
        ["git", "merge-base", "HEAD", "origin/master"], cwd=root, text=True).strip()
    tracked = subprocess.check_output(
        ["git", "diff", "--name-only", "-z", base], cwd=root).decode()
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root).decode()
    return set(filter(None, (tracked + untracked).split("\0")))


def prepare(root: Path, version: str, summary: str, slug: str, day: str,
            sources: set[str], dry_run: bool = False) -> dict:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must be major.minor.patch")
    date.fromisoformat(day)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("slug must use lowercase words separated by hyphens")
    summary = " ".join(summary.split())
    if not summary:
        raise ValueError("summary must not be empty")
    source = root / "src/interface_contract.py"
    guide = root / "docs/INTERFACE_CONTRACT.md"
    artifact = root / "docs/interface-contract.v1.json"
    fragment = root / f"docs/changelog.d/changed-{slug}.md"
    old_source, old_guide = source.read_text(), guide.read_text()
    matches = list(VERSION.finditer(old_source))
    if len(matches) != 1:
        raise ValueError("expected exactly one interface version declaration")
    old = matches[0].group(1)
    if tuple(map(int, version.split('.'))) <= tuple(map(int, old.split('.'))):
        raise ValueError(f"version must advance {old}")
    if fragment.exists():
        raise ValueError(f"refusing to overwrite {fragment}")
    current = f"version `{old}`"
    if old_guide.count(current) != 1:
        raise ValueError("current contract heading does not match source version")
    declaration = f'# {version} ({day}): {summary}\nINTERFACE_CONTRACT_VERSION = "{version}"'
    new_source = VERSION.sub(lambda _: declaration, old_source)
    entry = (f"\n## {version} ({day})\n\n{summary}\n\n"
             "<!-- Complete before readiness: affected outputs/descriptions, compatibility,\n"
             "     changed schema digests (or why none move), and client action. -->\n")
    new_guide = old_guide.replace(current, f"version `{version}`", 1) + entry
    writes = {source: new_source, guide: new_guide,
              fragment: f"- Interface contract {version}: {summary}\n"}
    all_sources = sources | {str(p.relative_to(root)) for p in (*writes, artifact)}
    result = {"from_version": old, "version": version,
              "files": [str(p.relative_to(root)) for p in (*writes, artifact)],
              "skills_to_recheck": affected_skills(root, all_sources),
              "dry_run": dry_run}
    if dry_run:
        return result
    # Restore every destination if generation/validation fails; preserve existing edits.
    originals = {p: p.read_bytes() if p.exists() else None for p in (*writes, artifact)}
    try:
        for path, content in writes.items():
            path.write_text(content)
        generated = subprocess.check_output(
            [sys.executable, "-m", "src.interface_contract"], cwd=root)
        payload = json.loads(generated)
        if payload.get("version") != version:
            raise ValueError("generated artifact did not use the requested version")
        artifact.write_bytes(generated)
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--source", action="append", default=[],
                        help="additional repo-relative source to include in skill impact")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(ROOT, args.version, args.summary, args.slug, args.date,
                         changed_sources(ROOT) | set(args.source), args.dry_run)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"interface release: {exc}\n")
    print(json.dumps(result, indent=2))
    print("Review the release entry; re-check listed skills, then explicitly --stamp them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
