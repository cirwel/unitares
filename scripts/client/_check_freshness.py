#!/usr/bin/env python3
"""Check skill freshness against the content of the sources each skill cites.

A skill's frontmatter declares `last_verified` (when its content was last
reviewed), `freshness_days`, and the `source_files` its claims depend on.
Verification records live beside the skills as attestations (below). Two checks:

- STALE: a cited source's current content matches no digest on record for
  the current skill text (see Attestations below), or a cited source has no
  recorded digest at all. The
  digest is computed from the file in the working tree, so the check needs no
  git history and gives the same answer in a fresh checkout, a shallow clone, a
  worktree, or an rsync copy. File mtime was the previous signal; every
  checkout rewrote it, so CI ran the calendar check only and five skills sat
  stale behind a green gate (2026-09-07). Commit dates were tried and rejected:
  the repository squash-merges, which stamps each landed change with its merge
  time, so a skill verified the day a change was authored would read stale the
  day the change merged.
- AGING: the skill was last verified longer ago than `freshness_days`, never
  below the FRESHNESS_FLOOR_DAYS floor, independent of any source.

Attestations
------------
`--stamp NAME [NAME ...]`, run after a skill's claims have been re-checked
against the changed sources, writes ONE NEW FILE per skill:

    skills/.attestations/<skill>/<YYYYMMDDTHHMMSSffffffZ>-<8 hex>.json
    {"schema": "unitares.skill_attestation.v1", "skill": ..., "verified_at":
     ISO-8601 UTC, "verified_date": "YYYY-MM-DD", "verifier": ...,
     "source_digests": {source: digest}, "skill_digest": digest of SKILL.md}

`skill_digest` (added 2026-09-24, optional for readers) names the skill text
the record certified.

It never edits SKILL.md. Until 2026-09-24 a stamp rewrote the `last_verified`
line and a `source_digests` block inside SKILL.md, and the skills manifest
hashed that file, so any two open pull requests that stamped conflicted on the
same lines: every merge put every other stamping PR back into conflict (five of
seven conflicted PRs that day conflicted only there). New files with unique
names cannot conflict, whoever writes them, from whichever harness.

A cited source is FRESH when its current digest equals the digest recorded
for it in any attestation whose `skill_digest` equals the current SKILL.md:
someone re-checked exactly this skill text against exactly that source
content. Only when no attestation certified the current text (a SKILL.md edit
not yet re-stamped, or records older than `skill_digest`) does the newest
attestation alone vouch, as before. The legacy frontmatter
`source_digests` block, which lives inside the current SKILL.md, also counts.
Until 2026-09-24 only the newest attestation (the lexically last file name)
counted, and that let a stamp mask a correct record: a branch cut from an
older master stamps the skill, recording OLD digests for sources it never
touched; the change to one of those sources merges first; the branch's
attestation then sorts newest and the skill reads STALE although an older
attestation records exactly the current content (observed on
unitares-governance after #2363 merged). Older records are scoped to the skill
text they certified because a digest verified for skill v1 says nothing about
v2: if v1 was stamped against source X, v2 against Y, and the source reverts
to X, v2 was never reviewed against X; nor does a v1 stamp from a concurrent
branch vouch for v2 because its file happens to sort newest.

The effective verified date, which drives AGING, comes from the same records
that vouch for source digests: the latest `verified_date` among them, or the
frontmatter `last_verified` if later. A newer stamp of different skill text
does not reset AGING for the text on disk. `.attestations/` is excluded from the skills
fingerprint (scripts/dev/skills_manifest.py), so the fingerprint moves only
when skill content moves. Old attestations can be removed with `--prune`;
deleting a file never conflicts with another PR adding one, but a pruned file
no longer vouches for its digests, so prune only what current content no
longer needs.

`--migrate` moves any `source_digests` block still in a SKILL.md frontmatter
into an attestation dated with that skill's `last_verified`.

Readers of the same format: this checker, the plugin repository's copy of it,
and the server's skills tool (src/mcp_handlers/introspection/skills.py).

Sources
-------
Cited paths are written from the projects directory (`unitares/src/x.py`,
`anima-mcp/...`). A path under `unitares/` resolves against this repository's
own root, so the check covers a worktree or clone whatever its directory is
called; before 2026-09-24 it resolved against the parent directory and passed
vacuously in any checkout not named `unitares`. Other repositories resolve
under the projects root, and one absent from this checkout is skipped and
counted in the report, not guessed. SKILL_FRESHNESS_AGE_ONLY=1 skips the source
check entirely.
"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
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
# authentication: 64 bits is far more than a skill's dozen sources need.
DIGEST_HEX = 16

ATTESTATIONS_DIR = ".attestations"
ATTESTATION_SCHEMA = "unitares.skill_attestation.v1"
THIS_REPO_PREFIX = "unitares/"

RED = "\033[0;31m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
NC = "\033[0m"

STAMP_HINT = "scripts/client/check-skill-freshness.sh --stamp"


def content_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:DIGEST_HEX]


def resolve_source(root: str, projects_root: str, src: str) -> Path:
    """`unitares/...` resolves against this repository; anything else against
    the projects root."""
    if src.startswith(THIS_REPO_PREFIX):
        return Path(root) / src[len(THIS_REPO_PREFIX):]
    return Path(projects_root) / src


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

    # Accept both layouts: nested `metadata.unitares.*` and flat top-level keys.
    meta = fm.get("metadata", {}) or {}
    last_verified = meta.get("unitares.last_verified") or fm.get("last_verified")
    freshness_days = meta.get("unitares.freshness_days") or fm.get("freshness_days")

    if not last_verified or not freshness_days:
        return {}

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


def load_attestations(skills_dir: Path, name: str) -> list[dict]:
    """Every readable attestation for a skill, newest first.

    Newest means the lexically last file name, which leads with a microsecond
    UTC timestamp. Unreadable files and records without a `source_digests`
    map are skipped.
    """
    adir = skills_dir / ATTESTATIONS_DIR / name
    if not adir.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(adir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("source_digests"), dict):
            records.append(data)
    return records


def latest_attestation(skills_dir: Path, name: str) -> dict | None:
    """The newest readable attestation for a skill, or None."""
    records = load_attestations(skills_dir, name)
    return records[0] if records else None


def effective_record(skills_dir: Path, name: str, meta: dict,
                     skill_digest: str | None = None) -> tuple[str, dict[str, set[str]]]:
    """(verified date, accepted digests per source).

    Which attestations vouch for source digests:
      * if any attestation certified the CURRENT skill text (its `skill_digest`
        equals ``skill_digest``), exactly those do, whatever their age, and no
        other. A record made against different skill text never vouches while
        a record for this text exists: skill v2 was never reviewed against the
        source content that v1 was, even if the v1 stamp sorts newest;
      * otherwise the current text has never been certified (a SKILL.md edit
        not yet re-stamped, or records older than `skill_digest`), and the
        newest attestation alone vouches, the rule before 2026-09-24.
    The legacy frontmatter block, which lives inside the current SKILL.md,
    always counts.

    The date comes from the SAME records: the newest `verified_date` among
    the vouching attestations, or the frontmatter `last_verified` if later.
    A stale branch stamping different skill text more recently must not reset
    AGING for the text actually on disk, since nobody re-verified that text.
    """
    accepted: dict[str, set[str]] = {}
    for src, digest in meta["source_digests"].items():
        accepted.setdefault(src, set()).add(digest)
    date = meta["last_verified"]
    records = load_attestations(skills_dir, name)
    certified = [a for a in records
                 if skill_digest is not None and a.get("skill_digest") == skill_digest]
    for att in certified or records[:1]:
        att_date = att.get("verified_date")
        if isinstance(att_date, str) and att_date > date:
            date = att_date
        for src, digest in att["source_digests"].items():
            accepted.setdefault(str(src), set()).add(str(digest))
    return date, accepted


def carried_digest(skills_dir: Path, name: str, meta: dict, src: str) -> str | None:
    """The most recent recorded digest for a source this checkout cannot see,
    so a stamp made here keeps the record made where it was visible."""
    for att in load_attestations(skills_dir, name):
        digest = att["source_digests"].get(src)
        if digest is not None:
            return str(digest)
    digest = meta["source_digests"].get(src)
    return str(digest) if digest is not None else None


def check_skills(root: str, projects_root: str) -> int:
    skills_dir = Path(root) / "skills"
    has_stale = False

    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        skill_name = skill_dir.name
        meta = parse_frontmatter(skill_file.read_text())

        if not meta:
            print(f"  [{YELLOW}-{NC}] {skill_name}: no freshness metadata")
            continue

        verified_date, accepted = effective_record(
            skills_dir, skill_name, meta, content_digest(skill_file))

        # Anchor to UTC so a CI runner (UTC) and a local machine (e.g. Mountain
        # Time) agree about day boundaries.
        max_days = max(meta["freshness_days"], FRESHNESS_FLOOR_DAYS)
        verified_start = datetime.strptime(verified_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - verified_start).days

        drift: tuple[str, str] | None = None
        absent = 0
        if not AGE_ONLY:
            for src in load_source_files(skill_dir, meta["source_files"]):
                full_path = resolve_source(root, projects_root, src)
                if not full_path.exists():
                    absent += 1
                    continue
                recorded = accepted.get(src)
                if not recorded:
                    drift = (src, "has no recorded digest")
                    break
                if content_digest(full_path) not in recorded:
                    drift = (src, f"changed since {verified_date}: no attestation "
                                  "records its current content")
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


def _verifier(root: str) -> str:
    """Who is attesting: an explicit override, else the git author identity."""
    explicit = os.environ.get("SKILL_ATTESTATION_VERIFIER", "").strip()
    if explicit:
        return explicit
    try:
        name = subprocess.run(
            ["git", "-C", root, "config", "user.name"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or "unknown"


def write_attestation(skills_dir: Path, name: str, digests: dict[str, str],
                      verified_at: datetime, verifier: str,
                      skill_digest: str | None = None) -> Path:
    adir = skills_dir / ATTESTATIONS_DIR / name
    adir.mkdir(parents=True, exist_ok=True)
    # Microseconds keep file-name order chronological for stamps inside the
    # same second; every reader takes the lexically last file as the newest,
    # so the random suffix must never be the tie-breaker.
    stem = f"{verified_at.strftime('%Y%m%dT%H%M%S%fZ')}-{secrets.token_hex(4)}"
    path = adir / f"{stem}.json"
    record = {
        "schema": ATTESTATION_SCHEMA,
        "skill": name,
        "verified_at": verified_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verified_date": verified_at.strftime("%Y-%m-%d"),
        "verifier": verifier,
        "source_digests": dict(sorted(digests.items())),
    }
    if skill_digest is not None:
        # The SKILL.md content this record certified. An older record keeps
        # vouching for its source digests only while the skill text is unchanged.
        record["skill_digest"] = skill_digest
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


def stamp_skills(root: str, projects_root: str, names: list[str]) -> int:
    skills_dir = Path(root) / "skills"
    now = datetime.now(timezone.utc)
    verifier = _verifier(root)
    rc = 0
    for name in names:
        skill_dir = skills_dir / name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            print(f"  [{RED}ERROR{NC}] {name}: no skills/{name}/SKILL.md")
            rc = 1
            continue
        meta = parse_frontmatter(skill_file.read_text())
        if not meta:
            print(f"  [{RED}ERROR{NC}] {name}: no freshness metadata to stamp")
            rc = 1
            continue
        digests: dict[str, str] = {}
        absent: list[str] = []
        for src in load_source_files(skill_dir, meta["source_files"]):
            full_path = resolve_source(root, projects_root, src)
            if full_path.exists():
                digests[src] = content_digest(full_path)
            elif (carried := carried_digest(skills_dir, name, meta, src)) is not None:
                # Not verifiable from here; keep the record made where it was.
                digests[src] = carried
            else:
                absent.append(src)
        path = write_attestation(skills_dir, name, digests, now, verifier,
                                 content_digest(skill_file))
        note = f", {len(absent)} absent source(s) left unrecorded" if absent else ""
        print(f"  stamped {name}: {path.relative_to(Path(root))}, {len(digests)} digest(s){note}")
    return rc


def strip_frontmatter_digests(content: str) -> str:
    """Remove a top-level `source_digests:` block from the frontmatter only."""
    if not content.startswith("---\n"):
        return content
    end = content.find("\n---", 4)
    if end == -1:
        return content
    lines = content[4:end].split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("source_digests:"):
            i += 1
            while i < len(lines) and lines[i][:1] in (" ", "\t"):
                i += 1
            continue
        kept.append(lines[i])
        i += 1
    return "---\n" + "\n".join(kept) + content[end:]


def migrate_skills(root: str) -> int:
    """Move frontmatter `source_digests` blocks into attestations."""
    skills_dir = Path(root) / "skills"
    moved = 0
    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        content = skill_file.read_text()
        meta = parse_frontmatter(content)
        if not meta or not meta["source_digests"]:
            continue
        verified_at = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        stripped = strip_frontmatter_digests(content)
        skill_file.write_text(stripped)
        path = write_attestation(skills_dir, skill_dir.name, meta["source_digests"],
                                 verified_at, "migrated from SKILL.md frontmatter",
                                 content_digest(skill_file))
        print(f"  migrated {skill_dir.name}: {len(meta['source_digests'])} digest(s) -> {path.relative_to(Path(root))}")
        moved += 1
    print(f"  {moved} skill(s) migrated")
    return 0


def prune_attestations(root: str, keep: int) -> int:
    """Delete all but the newest `keep` attestations per skill."""
    base = Path(root) / "skills" / ATTESTATIONS_DIR
    removed = 0
    if base.is_dir():
        for adir in sorted(p for p in base.iterdir() if p.is_dir()):
            for path in sorted(adir.glob("*.json"), reverse=True)[max(keep, 1):]:
                path.unlink()
                removed += 1
    print(f"  pruned {removed} attestation(s), kept the newest {max(keep, 1)} per skill")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("root", help="repository root; skills live in <root>/skills")
    parser.add_argument("projects_root", help="directory other repositories' cited paths are relative to")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--stamp", nargs="+", metavar="SKILL",
        help="write a new attestation with today's date and current source digests",
    )
    group.add_argument("--migrate", action="store_true",
                       help="move frontmatter source_digests blocks into attestations")
    group.add_argument("--prune", type=int, metavar="KEEP",
                       help="keep only the newest KEEP attestations per skill")
    args = parser.parse_args(argv)
    if args.stamp:
        return stamp_skills(args.root, args.projects_root, args.stamp)
    if args.migrate:
        return migrate_skills(args.root)
    if args.prune is not None:
        return prune_attestations(args.root, args.prune)
    return check_skills(args.root, args.projects_root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
