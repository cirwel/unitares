#!/usr/bin/env python3
"""Check skill freshness against source file modification times."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Calendar-age floor (days). A skill's per-skill `freshness_days` is honored, but
# the effective AGING threshold is never below this floor — so stable reference
# skills don't flip the whole gate red every couple of weeks on calendar time
# alone (the source-drift STALE check below still fires immediately on real
# source changes). Override with SKILL_FRESHNESS_FLOOR_DAYS.
FRESHNESS_FLOOR_DAYS = int(os.environ.get("SKILL_FRESHNESS_FLOOR_DAYS", "30"))

# Date-age-only mode. The source-drift check below compares last_verified to the
# *filesystem* mtime of each cited source file — meaningful locally (real edit
# times) but useless in CI: a fresh `actions/checkout` rewrites every file's
# mtime to checkout time, so any skill not verified on the run date would falsely
# flag STALE. CI sets SKILL_FRESHNESS_AGE_ONLY=1 to run the checkout-independent
# calendar-age check only; the source-drift check stays a local author-time hint.
# (Harmless in the plugin repo, where cited `unitares/...` paths are absent and
# the source check already no-ops — keeps this script identical across repos.)
AGE_ONLY = os.environ.get("SKILL_FRESHNESS_AGE_ONLY") == "1"

RED = "\033[0;31m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
NC = "\033[0m"


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

    return {
        "last_verified": str(last_verified),
        "freshness_days": int(freshness_days),
        "source_files": [str(f) for f in fm_sources],
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

        # Anchor everything to UTC so a CI runner (UTC) and a local machine
        # (e.g. Mountain Time) agree about day boundaries — otherwise the same
        # source mtime can read FRESH locally but STALE in CI near midnight.
        verified_date = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        max_days = max(meta["freshness_days"], FRESHNESS_FLOOR_DAYS)
        verified_date_start = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - verified_date_start).days

        # Source-drift check (local author-time hint; skipped in CI via AGE_ONLY).
        source_modified = False
        modified_file = ""
        if not AGE_ONLY:
            source_files = load_source_files(skill_dir, meta.get("source_files", []))
            for src in source_files:
                full_path = Path(projects_root) / src
                if full_path.exists():
                    mtime = datetime.fromtimestamp(os.path.getmtime(full_path), tz=timezone.utc)
                    if mtime > verified_date:
                        source_modified = True
                        modified_file = f"{src} (modified {mtime.strftime('%Y-%m-%d')})"
                        break

        if source_modified:
            print(f"  [{RED}STALE{NC}] {skill_name}: verified {meta['last_verified']}, but {modified_file}")
            has_stale = True
        elif age_days > max_days:
            print(f"  [{YELLOW}AGING{NC}] {skill_name}: verified {age_days} days ago (threshold: {max_days})")
            has_stale = True
        else:
            print(f"  [{GREEN}FRESH{NC}] {skill_name}: verified {age_days} days ago")

    if has_stale:
        print()
        print("Some skills are stale. Update last_verified after reviewing source changes.")
        return 1
    return 0


if __name__ == "__main__":
    plugin_root = sys.argv[1]
    projects_root = sys.argv[2]
    sys.exit(check_skills(plugin_root, projects_root))
