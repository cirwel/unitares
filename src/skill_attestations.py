"""Which skill attestations vouch for the skill text on disk.

Re-verifying a skill writes an attestation file,
`skills/.attestations/<skill>/<YYYYMMDDTHHMMSSffffffZ>-<8 hex>.json`, instead of
editing SKILL.md (format and writer: scripts/client/_check_freshness.py). A
record may carry `skill_digest`, the digest of the SKILL.md content it
certified. One rule decides which records speak for the CURRENT text, and two
readers apply it: the CI freshness checker (source digests and the AGING date)
and the server's `skills` tool (the served `last_verified`, `version` and
`stale`). They share this module so the rule cannot drift between them.

THE RULE: if any attestation certified the current text (its `skill_digest`
equals the current SKILL.md's digest), exactly those vouch, whatever their age.
Otherwise the current text has never been certified (a SKILL.md edit not yet
re-stamped, or records older than `skill_digest`), and the newest attestation
alone vouches, the rule before 2026-09-24. A record for other skill text never
vouches while one for this text exists: skill v2 was never reviewed against
what v1 was reviewed against, even when a v1 stamp from a stale branch sorts
newest.

Standard library only: the checker runs this outside the server's environment.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ATTESTATIONS_DIR = ".attestations"

# Hex characters of sha256 kept per digest. Change detection, not
# authentication. Source digests use the same width.
DIGEST_HEX = 16


def skill_text_digest(skill_md: Path) -> str:
    """Digest of a SKILL.md as recorded in an attestation's `skill_digest`."""
    return hashlib.sha256(skill_md.read_bytes()).hexdigest()[:DIGEST_HEX]


def load_attestations(skills_root: Path, name: str) -> list[dict]:
    """Every readable attestation for a skill, newest first.

    Newest means the lexically last file name, which leads with a microsecond
    UTC timestamp. Unreadable files and records without a `source_digests`
    map are skipped.
    """
    adir = skills_root / ATTESTATIONS_DIR / name
    if not adir.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(adir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("source_digests"), dict):
            records.append(data)
    return records


def certified_attestations(records: list[dict], skill_digest: str | None) -> list[dict]:
    """The records that certified exactly the current skill text."""
    if skill_digest is None:
        return []
    return [r for r in records if r.get("skill_digest") == skill_digest]


def vouching_attestations(records: list[dict], skill_digest: str | None) -> list[dict]:
    """The records that speak for the current skill text (see THE RULE)."""
    return certified_attestations(records, skill_digest) or records[:1]


def vouching_date(records: list[dict], skill_digest: str | None,
                  floor: str | None = None) -> str | None:
    """Newest `verified_date` among the vouching records, or ``floor`` (the
    frontmatter `last_verified`) if that is later."""
    date = floor
    for record in vouching_attestations(records, skill_digest):
        verified = record.get("verified_date")
        if isinstance(verified, str) and verified and (date is None or verified > date):
            date = verified
    return date
