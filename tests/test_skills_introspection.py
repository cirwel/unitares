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


def test_compute_stale_reads_in_utc_not_the_local_clock(monkeypatch):
    """Pin the UTC/local disagreement, not just the UTC reading.

    `scripts/client/_check_freshness.py` stamps `last_verified` in UTC.
    `_compute_stale` used to read it back with a local `date.today()`, so on a
    host behind UTC every age came out one day short and the declared
    freshness window silently widened.

    Deriving the fixtures from the current UTC date cannot catch that: on a
    UTC host — which is what CI runs — the two clocks agree and the old
    implementation passes. Both clocks are frozen here, and disagreeing, so
    the last assertion fails against `date.today()`.
    """
    from datetime import date, datetime, timezone

    from src.mcp_handlers.introspection import skills as skills_mod

    class _LocalDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 19)  # host is behind UTC

    class _UtcClock:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 9, 20, 0, 30, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(skills_mod, "date", _LocalDate)
    monkeypatch.setattr(skills_mod, "datetime", _UtcClock)

    # Stamped today in UTC: fresh either way.
    assert skills_mod._compute_stale("2026-09-20", 7) is False
    # Exactly the window edge in UTC (7 days): fresh, and not yet stale.
    assert skills_mod._compute_stale("2026-09-13", 7) is False
    # 8 days old in UTC, but only 7 under the local clock. This is the
    # assertion the old `date.today()` implementation gets wrong.
    assert skills_mod._compute_stale("2026-09-12", 7) is True


def test_load_skill_uses_the_later_of_frontmatter_and_attestation_dates(tmp_path):
    """Re-verification writes an attestation file instead of editing SKILL.md
    (scripts/client/_check_freshness.py), so the served date must read it."""
    import json as _json

    from src.mcp_handlers.introspection import skills as skills_mod

    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\nname: demo\nlast_verified: "2026-01-01"\nfreshness_days: 14\n---\n# Demo\n'
    )
    assert skills_mod._load_skill(skill_dir)["last_verified"] == "2026-01-01"

    adir = tmp_path / ".attestations" / "demo"
    adir.mkdir(parents=True)
    (adir / "20260301T000000Z-aaaaaaaa.json").write_text(
        _json.dumps({"verified_date": "2026-03-01", "source_digests": {}})
    )
    (adir / "20260201T000000Z-bbbbbbbb.json").write_text(
        _json.dumps({"verified_date": "2026-02-01", "source_digests": {}})
    )
    loaded = skills_mod._load_skill(skill_dir)
    assert loaded["last_verified"] == "2026-03-01"
    assert loaded["version"] == "2026-03-01"


def test_load_skill_takes_the_newest_date_whatever_the_file_order(tmp_path):
    """Every attestation is read; the latest `verified_date` wins even when the
    lexically last file carries an older one."""
    import json as _json

    from src.mcp_handlers.introspection import skills as skills_mod

    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\nname: demo\nlast_verified: "2026-01-01"\nfreshness_days: 14\n---\n# Demo\n'
    )
    adir = tmp_path / ".attestations" / "demo"
    adir.mkdir(parents=True)
    (adir / "20260101T000000000000Z-aaaaaaaa.json").write_text(
        _json.dumps({"verified_date": "2026-03-01", "source_digests": {}})
    )
    (adir / "20260102T000000000000Z-bbbbbbbb.json").write_text(
        _json.dumps({"verified_date": "2026-02-01", "source_digests": {}})
    )
    (adir / "20260103T000000000000Z-cccccccc.json").write_text("{not json")
    assert skills_mod._load_skill(skill_dir)["last_verified"] == "2026-03-01"


def _load_manifest_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "skills_manifest", Path(__file__).resolve().parents[1] / "scripts/dev/skills_manifest.py"
    )
    manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest)
    return manifest


def test_manifest_ignores_attestation_files(tmp_path, monkeypatch):
    manifest = _load_manifest_module()
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "SKILL.md").write_text("# Demo\n")
    monkeypatch.setattr(manifest, "SKILLS_DIR", tmp_path)
    before = manifest.build_manifest()
    (tmp_path / ".attestations" / "demo").mkdir(parents=True)
    (tmp_path / ".attestations" / "demo" / "20260924T000000Z-cccccccc.json").write_text("{}")
    assert manifest.build_manifest() == before


def test_manifest_ignores_a_manifest_file_in_the_tree(tmp_path):
    """A stray regeneration in canonical, or the mirror's own copy, is not
    part of the fingerprint, so a mirror verifies against itself."""
    manifest = _load_manifest_module()
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "SKILL.md").write_text("# Demo\n")
    before = manifest.build_manifest(tmp_path)
    (tmp_path / manifest.MANIFEST_NAME).write_text(before)
    assert manifest.build_manifest(tmp_path) == before
    assert manifest.main(["x", "--skills-dir", str(tmp_path),
                          "--verify", str(tmp_path / manifest.MANIFEST_NAME)]) == 0
    (tmp_path / "demo" / "SKILL.md").write_text("# Demo, edited\n")
    assert manifest.main(["x", "--skills-dir", str(tmp_path),
                          "--verify", str(tmp_path / manifest.MANIFEST_NAME)]) == 1


def test_manifest_is_not_committed_in_unitares():
    """The fingerprint is derived data, generated where it is consumed
    (scripts/dev/sync-plugin-skills.sh writes it into the plugin mirror).
    Committed, its aggregate line made any two PRs editing any two skills
    conflict (#2361 against #2363, 2026-09-24). Re-adding it, even with
    `git add -f` past .gitignore, brings that back."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("not a git checkout")
    rel = _load_manifest_module().MANIFEST_PATH.relative_to(root).as_posix()
    assert rel == "skills/SKILLS_MANIFEST.sha256"
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--", rel],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert tracked == "", f"{rel} is tracked again; it must stay generated, not committed"
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--no-index", rel])
    assert ignored.returncode == 0, f"{rel} must stay in .gitignore"
