#!/usr/bin/env python3
"""Check skill freshness against source file modification times."""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

RED = "\033[0;31m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
NC = "\033[0m"


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from skill file."""
    fm = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not fm:
        return {}
    fm_text = fm.group(1)

    verified_m = re.search(r'last_verified:\s*["\'](\d{4}-\d{2}-\d{2})["\']', fm_text)
    days_m = re.search(r"freshness_days:\s*(\d+)", fm_text)
    sources = re.findall(r"^\s+-\s+(.+)$", fm_text, re.MULTILINE)

    if not verified_m or not days_m:
        return {}

    return {
        "last_verified": verified_m.group(1),
        "freshness_days": int(days_m.group(1)),
        "source_files": [s.strip() for s in sources],
    }


def check_skills(plugin_root: str, projects_root: str) -> int:
    skills_dir = Path(plugin_root) / "skills"
    has_stale = False

    # The source-file check below compares last_verified to the *filesystem*
    # mtime of each cited source file. That is meaningful locally (real edit
    # times) but useless in CI: a fresh `actions/checkout` rewrites every file's
    # mtime to checkout time, so every skill not verified on the CI run's exact
    # date would falsely flag STALE. CI therefore sets SKILL_FRESHNESS_AGE_ONLY=1
    # to run the checkout-independent date-age check only; the richer source
    # check stays a local author-time hint (ship.sh / pre-commit).
    age_only = os.environ.get("SKILL_FRESHNESS_AGE_ONLY") == "1"

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

        verified_date = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        max_days = meta["freshness_days"]
        verified_date_start = datetime.strptime(meta["last_verified"], "%Y-%m-%d")
        age_days = (datetime.now() - verified_date_start).days

        # Check if source files were modified after last_verified (local only —
        # filesystem mtime is unreliable in CI; see age_only note above).
        source_modified = False
        modified_file = ""
        if not age_only:
            for src in meta["source_files"]:
                full_path = Path(projects_root) / src
                if full_path.exists():
                    mtime = datetime.fromtimestamp(os.path.getmtime(full_path))
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
