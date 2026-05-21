"""Tests for the `skills` MCP introspection handler.

+ Appendix.

The handler reads `unitares/skills/*/SKILL.md`, parses YAML frontmatter,
and returns a structured response keyed by skill name + version. It does
NOT consume any agent identity — see §4.5 (identity-blindness invariant).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp.types import TextContent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _call_handler(arguments: dict) -> dict:
    """Invoke the skills handler synchronously and return the parsed payload."""
    from src.mcp_handlers.introspection.skills import handle_skills

    result = asyncio.run(handle_skills(arguments))
    assert isinstance(result, list) and len(result) > 0, "expected non-empty TextContent list"
    assert isinstance(result[0], TextContent)
    return json.loads(result[0].text)


# ---------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------

def test_handler_returns_skills_array():
    payload = _call_handler({})
    assert payload["success"] is True
    assert "skills" in payload
    assert isinstance(payload["skills"], list)
    assert len(payload["skills"]) > 0, (
        "skills/ directory has 6 skills; handler must return at least one"
    )


def test_handler_parses_known_skill_frontmatter():
    """`governance-fundamentals` is a known canonical skill; verify its frontmatter parses."""
    payload = _call_handler({})
    names = {s["name"] for s in payload["skills"]}
    assert "governance-fundamentals" in names, (
        f"governance-fundamentals not in response; got {names}"
    )
    skill = next(s for s in payload["skills"] if s["name"] == "governance-fundamentals")
    assert skill["description"], "description must be non-empty"
    assert "last_verified" in skill, "frontmatter must surface last_verified"
    assert "freshness_days" in skill, "frontmatter must surface freshness_days"
    assert "source_files" in skill, "frontmatter must surface source_files"
    assert isinstance(skill["source_files"], list)
    assert "content" in skill, "skill must include the markdown body"
    assert "content_hash" in skill, "skill must include content_hash for cache invalidation"


def test_handler_returns_registry_version_and_hash():
    """Top-level fields per §4.5 — version + hash drive client cache invalidation."""
    payload = _call_handler({})
    assert "registry_version" in payload, "top-level registry_version required"
    assert "registry_hash" in payload, "top-level registry_hash required"
    assert payload["registry_hash"].startswith("sha256:"), (
        "registry_hash must be sha256-prefixed for transparency"
    )


# ---------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------

def test_handler_filters_by_name_param():
    """`name=<skill>` returns only that skill."""
    payload = _call_handler({"name": "governance-fundamentals"})
    assert len(payload["skills"]) == 1
    assert payload["skills"][0]["name"] == "governance-fundamentals"


def test_handler_unknown_name_returns_empty_skills():
    """Asking for a skill that doesn't exist must not error — returns empty array."""
    payload = _call_handler({"name": "no-such-skill-zzzz"})
    assert payload["success"] is True
    assert payload["skills"] == []


def test_handler_filters_by_since_version_future():
    """`since_version` newer than registry returns empty (cheap re-poll case)."""
    payload = _call_handler({"since_version": "2099-12-31"})
    assert payload["success"] is True
    assert payload["skills"] == [], (
        "since_version in the future means no skills updated since; expect empty array"
    )


def test_handler_filters_by_since_version_past_returns_all():
    """`since_version` older than every skill returns the full bundle."""
    payload = _call_handler({"since_version": "1970-01-01"})
    assert payload["success"] is True
    assert len(payload["skills"]) > 0


# ---------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------

def test_handler_registry_hash_is_deterministic():
    """Same input on disk → same registry_hash. No timestamps, no agent state."""
    p1 = _call_handler({})
    p2 = _call_handler({})
    assert p1["registry_hash"] == p2["registry_hash"]
    assert p1["registry_version"] == p2["registry_version"]


def test_handler_content_hash_is_per_skill_stable():
    p1 = _call_handler({})
    p2 = _call_handler({})
    h1 = {s["name"]: s["content_hash"] for s in p1["skills"]}
    h2 = {s["name"]: s["content_hash"] for s in p2["skills"]}
    assert h1 == h2


# ---------------------------------------------------------------------
# Identity-blindness (§4.5 invariant — load-bearing)
# ---------------------------------------------------------------------

def test_handler_response_does_not_leak_agent_identity():
    """Skills are content-addressed. The response must not include any agent-
    derived value — no agent_uuid, no agent_id, no client_session_id at the
    skill level. This is the §4.5 invariant guarding against future
    'personalize per agent' optimizations that would smuggle identity into a
    structurally identity-blind surface."""
    payload = _call_handler({})
    forbidden_keys = {"agent_uuid", "agent_id", "client_session_id", "continuity_token"}
    for skill in payload["skills"]:
        for k in skill.keys():
            assert k not in forbidden_keys, (
                f"identity-blindness violation: skill {skill.get('name')} contains {k}"
            )
    # Top-level too — agent_signature is added by success_response when agent_id passed,
    # but the handler must not be passing agent_id.
    assert "agent_signature" not in payload, (
        "skills handler must not pass agent_id to success_response — would taint cache key"
    )


def test_handler_ignores_identity_arguments():
    """If a caller passes agent_uuid or client_session_id, the handler must
    return identical content (cache must not vary by identity)."""
    p_anon = _call_handler({})
    p_with_id = _call_handler({
        "agent_uuid": "00000000-0000-0000-0000-000000000000",
        "client_session_id": "irrelevant",
    })
    assert p_anon["registry_hash"] == p_with_id["registry_hash"], (
        "identity arguments must not change the response shape"
    )


# ---------------------------------------------------------------------
# Stale flag (§4.4)
# ---------------------------------------------------------------------

def test_handler_returns_stale_flag_per_skill():
    """Each skill must carry a `stale: bool` field. Computation is git-log against
    source_files vs last_verified; result is bool either way."""
    payload = _call_handler({})
    for skill in payload["skills"]:
        assert "stale" in skill, f"skill {skill['name']} missing stale flag"
        assert isinstance(skill["stale"], bool)
