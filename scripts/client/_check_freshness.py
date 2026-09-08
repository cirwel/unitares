#!/usr/bin/env python3
"""Check skill freshness against the content of the sources each skill cites.

A skill's frontmatter declares `last_verified`, `freshness_days`, the
`source_files` its claims depend on, and `source_digests`: a content digest of
each cited source taken when the skill was last verified. Two checks:

- STALE: a cited source's content no longer matches its recorded digest, or a
  cited source has no recorded digest. The digest is computed from the file in
  the working tree, so the check needs no git history and gives the same
  answer in a fresh checkout, a shallow clone, a worktree, or an rsync copy.
  File mtime was the previous signal; every checkout rewrote it, so CI ran the
  calendar check only and five skills sat stale behind a green gate
  (2026-09-07). Commit dates were tried and rejected: the repository
  squash-merges, which stamps each landed change with its merge time, so a
  skill verified the day a change was authored would read stale the day the
  change merged.
- AGING: `last_verified` is older than `freshness_days`, never below the
  FRESHNESS_FLOOR_DAYS floor, independent of any source.

A cited source absent from this checkout (another repository) is skipped and
counted in the report, not guessed. SKILL_FRESHNESS_AGE_ONLY=1 skips the
source check entirely; it is an explicit override, no longer something CI
needs.

`--stamp NAME [NAME ...]` records today's date and the current digests for the
named skills, to run after their claims have been re-checked against the
changed sources. It rewrites only the `last_verified` line and the
`source_digests` block; every other byte of the file is preserved.

Identical across the unitares and plugin repositories: in the plugin the cited
`unitares/...` paths are absent and the source check no-ops.
"""

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Calendar-age floor (days). A skill's per-skill `freshness_days` is honored, but
# the effective AGING threshold is never below this floor -- so stable reference
# skills don't flip the whole gate red every couple of weeks on calendar time
# alone (the source-drift STALE check still fires on a real source change).
# Override with SKILL_FRESHNESS_FLOOR_DAYS.
FRESHNESS_FLOOR_DAYS = int(os.environ.get("SKILL_FRESHNESS_FLOOR_DAYS", "30"))

# Explicit override: calendar age only, no source check at all.
AGE_ONLY = os.environ.get("SKILL_FRESHNESS_AGE_ONLY") == "1"

# Hex characters of sha256 recorded per source. Change detection, not
# authentication: 64 bits is far more than a skill's dozen sources need, and
# keeps the frontmatter block readable.
DIGEST_HEX = 16

RED = "\033[0;31m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
NC = "\033[0m"

STAMP_HINT = "scripts/client/check-skill-freshness.sh --stamp"


def content_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:DIGEST_HEX]


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from skill file."""
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---", 4)
    if end == -1:
        return {}
    fm = yaml.safe_load(content[4:end])
    if not isinstance(fm, dict):
        return {}

    # Accept both layouts: nested `metadata.unitares.*` (current) and flat
    # top-level keys (the in-progress frontmatter refactor). Without the flat
    # fallback the parser would silently return {} on refactored skills and the
    # gate would stop checking them.
    meta = fm.get("metadata", {}) or {}
    last_verified = meta.get("unitares.last_verified") or fm.get("last_verified")
    freshness_days = meta.get("unitares.freshness_days") or fm.get("freshness_days")

    if not last_verified or not freshness_days:
        return {}

    # source_files may live in the flat frontmatter (refactor) instead of the
    # .freshness.yaml sidecar; surface it so the STALE drift check still works.
    fm_sources = fm.get("source_files") or []
    fm_digests = fm.get("source_digests") or {}
    if not isinstance(fm_digests, dict):
        fm_digests = {}

    return {
        "last_verified": str(last_verified),
        "freshness_days": int(freshness_days),
        "source_files": [str(f) for f in fm_sources],
        "source_digests": {str(k): str(v) for k, v in fm_digests.items()},
    }


def load_source_files(skill_dir: Path, frontmatter_sources: list[str]) -> list[str]:
    """source_files from the .freshness.yaml sidecar, else from frontmatter."""
    sidecar = skill_dir / ".freshness.yaml"
    if sidecar.exists():
        data = yaml.safe_load(sidecar.read_text())
        if isinstance(data, dict):
            files = data.get("source_files", [])
            if files:
                return [str(f) for f in files]
    return list(frontmatter_sources)


def check_skills(plugin_root: str, projects_root: str) -> int:
    skills_dir = Path(plugin_root) / "skills"
    has_stale = False

    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        skill_name = skill_dir.name
        content = skill_file.read_text()
        meta = parse_frontmatter(content)

        if not meta:
            print(f"  [{YELLOW}-{NC}] {skill_name}: no freshness metadata")
            continue

        # Anchor to UTC so a CI runner (UTC) and a local machine (e.g. Mountain
        # Time) agree about day boundaries.
        max_days = max(meta["freshness_days"], FRESHNESS_FLOOR_DAYS)
        verified_date_start = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - verified_date_start).days

        # Source-drift check: recorded digest vs working-tree content (see the
        # module docstring for why neither mtime nor commit dates are used).
        drift: tuple[str, str] | None = None
        absent = 0
        if not AGE_ONLY:
            for src in load_source_files(skill_dir, meta["source_files"]):
                full_path = Path(projects_root) / src
                if not full_path.exists():
                    absent += 1
                    continue
                recorded = meta["source_digests"].get(src)
                if recorded is None:
                    drift = (src, "has no recorded digest")
                    break
                if content_digest(full_path) != recorded:
                    drift = (src, f"changed since {meta['last_verified']}")
                    break

        if drift:
            src, reason = drift
            print(f"  [{RED}STALE{NC}] {skill_name}: {src} {reason}")
            print(f"          re-check the claims that cite it, then: {STAMP_HINT} {skill_name}")
            has_stale = True
        elif age_days > max_days:
            print(f"  [{YELLOW}AGING{NC}] {skill_name}: verified {age_days} days ago (threshold: {max_days})")
            has_stale = True
        else:
            note = ""
            if absent:
                note = f"; {absent} cited source(s) absent from this checkout, not covered"
            print(f"  [{GREEN}FRESH{NC}] {skill_name}: verified {age_days} days ago{note}")

    if has_stale:
        print()
        print("Some skills are stale. Re-check each one against the change it names, then --stamp it.")
        return 1
    return 0


def rewrite_frontmatter(content: str, today: str, digests: dict[str, str]) -> str:
    """Replace the `last_verified` line and the `source_digests` block, nothing else."""
    if not content.startswith("---\n"):
        raise ValueError("no frontmatter")
    end = content.find("\n---", 4)
    if end == -1:
        raise ValueError("unterminated frontmatter")
    lines = content[4:end].split("\n")

    kept: list[str] = []
    replaced_date = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^last_verified:", line):
            kept.append(f'last_verified: "{today}"')
            replaced_date = True
            i += 1
            continue
        if re.match(r"^source_digests:", line):
            i += 1
            while i < len(lines) and lines[i][:1] in (" ", "\t"):
                i += 1
            continue
        kept.append(line)
        i += 1
    if not replaced_date:
        raise ValueError("no top-level last_verified line")

    if digests:
        block = ["source_digests:"] + [f'  {src}: "{d}"' for src, d in digests.items()]
        insert_at = len(kept)
        for j, line in enumerate(kept):
            if re.match(r"^source_files:", line):
                k = j + 1
                while k < len(kept) and kept[k][:1] in (" ", "\t"):
                    k += 1
                insert_at = k
                break
        kept[insert_at:insert_at] = block

    return "---\n" + "\n".join(kept) + content[end:]


def stamp_skills(plugin_root: str, projects_root: str, names: list[str]) -> int:
    skills_dir = Path(plugin_root) / "skills"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rc = 0
    for name in names:
        skill_dir = skills_dir / name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            print(f"  [{RED}ERROR{NC}] {name}: no skills/{name}/SKILL.md")
            rc = 1
            continue
        content = skill_file.read_text()
        meta = parse_frontmatter(content)
        if not meta:
            print(f"  [{RED}ERROR{NC}] {name}: no freshness metadata to stamp")
            rc = 1
            continue
        digests: dict[str, str] = {}
        absent: list[str] = []
        for src in load_source_files(skill_dir, meta["source_files"]):
            full_path = Path(projects_root) / src
            if full_path.exists():
                digests[src] = content_digest(full_path)
            elif src in meta["source_digests"]:
                # Not verifiable from here; keep the record made where it was.
                digests[src] = meta["source_digests"][src]
            else:
                absent.append(src)
        skill_file.write_text(rewrite_frontmatter(content, today, digests))
        note = f", {len(absent)} absent source(s) left unrecorded" if absent else ""
        print(f"  stamped {name}: last_verified {today}, {len(digests)} digest(s){note}")
    return rc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("root", help="repository root; skills live in <root>/skills")
    parser.add_argument("projects_root", help="directory the cited source paths are relative to")
    parser.add_argument(
        "--stamp", nargs="+", metavar="SKILL",
        help="record today's date and the current source digests for these skills",
    )
    args = parser.parse_args(argv)
    if args.stamp:
        return stamp_skills(args.root, args.projects_root, args.stamp)
    return check_skills(args.root, args.projects_root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
