"""Server-side `skills` MCP introspection handler.

Per docs/ontology/s15-server-side-skills.md (S15-a). Reads canonical skill
content from `unitares/skills/*/SKILL.md`, parses YAML frontmatter, and
returns a structured response keyed by skill name + content hash.

Identity-blindness invariant (§4.5): the handler does NOT consume agent
identity. Identity-derived arguments are accepted but ignored — same
content for every caller. Cache key downstream is content-addressed.

The handler reads from disk on every call. Files total ~50KB, no DB
involvement, so the anyio-asyncio mitigation patterns from CLAUDE.md
do not apply (no `await` against asyncpg/Redis in this path).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml
from mcp.types import TextContent

from src.logging_utils import get_logger

from ..decorators import mcp_tool
from ..response_base import success_response

logger = get_logger(__name__)

# Resolve skills/ relative to repo root (this file lives at
# <repo>/src/mcp_handlers/introspection/skills.py — go up four levels).
_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


# ---------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter_dict, body_markdown).

    Format: leading `---\\n`, YAML block, closing `---\\n`, then body.
    Files without frontmatter return ({}, full_text).
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw_yaml = text[4:end]
    body = text[end + 5 :]
    try:
        meta = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        logger.warning("[SKILLS] frontmatter parse error: %s", exc)
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def _content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------

def _load_skill(skill_dir: Path) -> Optional[Dict[str, Any]]:
    """Load and parse one skill directory. Returns None if unloadable."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[SKILLS] could not read %s: %s", skill_md, exc)
        return None

    meta, body = _split_frontmatter(text)

    # Name falls back to directory name if frontmatter missing it.
    name = meta.get("name") or skill_dir.name
    last_verified = meta.get("last_verified")
    if isinstance(last_verified, date):
        last_verified = last_verified.isoformat()
    elif last_verified is not None:
        last_verified = str(last_verified)

    source_files = meta.get("source_files") or []
    if not isinstance(source_files, list):
        source_files = []

    triggers = meta.get("triggers")
    if not isinstance(triggers, dict):
        triggers = None

    skill: Dict[str, Any] = {
        "name": str(name),
        "description": str(meta.get("description") or "").strip(),
        "version": last_verified,  # ISO date doubles as per-skill version
        "last_verified": last_verified,
        "freshness_days": meta.get("freshness_days"),
        "source_files": [str(s) for s in source_files],
        "triggers": triggers,
        "stale": _compute_stale(last_verified, meta.get("freshness_days")),
        "content": body,
        "content_hash": _content_hash(body),
    }
    return skill


def _compute_stale(last_verified: Optional[str], freshness_days: Any) -> bool:
    """Compute stale flag based on age of last_verified vs freshness_days.

    Conservative — uses date arithmetic, not git log against source_files.
    git-log staleness is a future enhancement; the date check covers the
    common case (skill not touched in N days).
    """
    if not last_verified:
        return False
    try:
        verified = date.fromisoformat(last_verified)
    except (ValueError, TypeError):
        return False
    if not isinstance(freshness_days, (int, float)):
        return False
    age = (date.today() - verified).days
    return age > int(freshness_days)


def _load_all_skills() -> List[Dict[str, Any]]:
    if not _SKILLS_ROOT.is_dir():
        return []
    skills: List[Dict[str, Any]] = []
    for entry in sorted(_SKILLS_ROOT.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill = _load_skill(entry)
        if skill is not None:
            skills.append(skill)
    return skills


def _registry_hash(skills: List[Dict[str, Any]]) -> str:
    """Deterministic hash over the canonical-ordered (name, content_hash) pairs.

    Independent of any caller-supplied state. Stable across processes.
    """
    canonical = [(s["name"], s["content_hash"]) for s in sorted(skills, key=lambda s: s["name"])]
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


def _registry_version(skills: List[Dict[str, Any]]) -> Optional[str]:
    """Max last_verified across all skills. Drives client cache-invalidation."""
    dates = [s["last_verified"] for s in skills if s.get("last_verified")]
    return max(dates) if dates else None


# ---------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------

def _filter_skills(
    skills: List[Dict[str, Any]],
    *,
    name: Optional[str],
    since_version: Optional[str],
) -> List[Dict[str, Any]]:
    out = skills
    if name:
        out = [s for s in out if s["name"] == name]
    if since_version:
        out = [
            s for s in out
            if s.get("last_verified") and s["last_verified"] > since_version
        ]
    return out


# ---------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------

@mcp_tool("skills", timeout=10.0, rate_limit_exempt=True, requires_identity="pre_onboard")
async def handle_skills(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Return server-authored skill bundle for adapter consumption.

    Parameters:
        name (str, optional): Return only the skill with this exact name.
        since_version (str, optional): ISO date; return only skills with
            last_verified > since_version. Used for cheap re-poll cache
            invalidation by client adapters.

    Identity-blind: identity-derived arguments are silently ignored (§4.5).

    Returns response with shape:
        {
          "skills": [{name, description, version, last_verified, ...}],
          "registry_version": "<max last_verified>",
          "registry_hash": "sha256:..."
        }
    """
    args = arguments or {}
    name = args.get("name") if isinstance(args.get("name"), str) else None
    since_version = (
        args.get("since_version")
        if isinstance(args.get("since_version"), str)
        else None
    )

    all_skills = _load_all_skills()
    registry_hash = _registry_hash(all_skills)
    registry_version = _registry_version(all_skills)
    filtered = _filter_skills(all_skills, name=name, since_version=since_version)

    data = {
        "skills": filtered,
        "registry_version": registry_version,
        "registry_hash": registry_hash,
    }
    # lite_response=True suppresses the default `agent_signature: {"uuid": None}`
    # block — without it, even an unbound caller gets an identity-shaped field
    # in the response, which violates the §4.5 identity-blindness invariant
    # (skills are content-addressed; cache keys must not vary by identity).
    return success_response(data, arguments={"lite_response": True})
