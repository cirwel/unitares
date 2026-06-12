"""CI tests for the violation taxonomy.

Tests enforce:
- YAML parses and loads correctly
- All class IDs are unique and active
- Surface IDs are unique across classes (no duplicates)
- Reverse lookup works for all surfaces
- validate_class_id accepts active, rejects unknown
- Convenience wrappers return correct classes
"""

import pytest
from src.violation_taxonomy import (
    load_taxonomy,
    get_taxonomy,
    validate_class_id,
    validate_surface_mapping,
    lookup_class_for_surface,
    class_for_watcher_pattern,
    class_for_sentinel_finding,
    class_for_broadcast_event,
)


def test_load_taxonomy_returns_dict():
    tax = load_taxonomy()
    assert isinstance(tax, dict)
    assert tax["version"] == 1
    assert tax["kind"] == "unitares_violation_taxonomy"


def test_all_classes_have_required_fields():
    tax = load_taxonomy()
    for cls in tax["classes"]:
        assert "id" in cls
        assert "status" in cls
        assert "name" in cls
        assert "description" in cls
        assert "surfaces" in cls
        surfaces = cls["surfaces"]
        assert "watcher_patterns" in surfaces
        assert "sentinel_findings" in surfaces
        assert "broadcast_events" in surfaces


def test_all_class_ids_unique():
    tax = load_taxonomy()
    ids = [c["id"] for c in tax["classes"]]
    assert len(ids) == len(set(ids)), f"Duplicate class IDs: {ids}"


def test_surface_ids_unique_across_classes():
    """Each surface ID must appear in at most one class."""
    tax = load_taxonomy()
    seen: dict[str, str] = {}  # surface_id -> class_id
    for cls in tax["classes"]:
        for kind in ("watcher_patterns", "sentinel_findings", "broadcast_events"):
            for sid in cls["surfaces"].get(kind, []):
                assert sid not in seen, (
                    f"Surface '{sid}' in both {seen[sid]} and {cls['id']}"
                )
                seen[sid] = cls["id"]


def test_validate_class_id_accepts_active():
    assert validate_class_id("CON") is True
    assert validate_class_id("INT") is True
    assert validate_class_id("ENT") is True
    assert validate_class_id("REC") is True
    assert validate_class_id("BEH") is True
    assert validate_class_id("VOI") is True


def test_validate_class_id_rejects_unknown():
    assert validate_class_id("FAKE") is False
    assert validate_class_id("") is False


def test_reverse_lookup_watcher_patterns():
    assert class_for_watcher_pattern("P001") == "ENT"
    assert class_for_watcher_pattern("P004") == "REC"
    assert class_for_watcher_pattern("P011") == "INT"
    assert class_for_watcher_pattern("P006") == "VOI"
    assert class_for_watcher_pattern("P999") is None


def test_reverse_lookup_sentinel_findings():
    assert class_for_sentinel_finding("coordinated_degradation") == "CON"
    assert class_for_sentinel_finding("entropy_outlier") == "ENT"
    assert class_for_sentinel_finding("correlated_events") == "BEH"
    assert class_for_sentinel_finding("nonexistent") is None


def test_reverse_lookup_broadcast_events():
    assert class_for_broadcast_event("identity_assurance_change") == "CON"
    assert class_for_broadcast_event("circuit_breaker_trip") == "REC"
    assert class_for_broadcast_event("knowledge_confidence_clamped") == "INT"
    assert class_for_broadcast_event("nonexistent") is None


def test_validate_surface_mapping():
    assert validate_surface_mapping("watcher_patterns", "P001") is True
    assert validate_surface_mapping("sentinel_findings", "entropy_outlier") is True
    assert validate_surface_mapping("watcher_patterns", "P999") is False


def test_get_taxonomy_caches():
    t1 = get_taxonomy()
    t2 = get_taxonomy()
    assert t1 is t2


# ---------------------------------------------------------------------------
# CI consistency: orphan detection and bidirectional coverage
# ---------------------------------------------------------------------------


def test_all_watcher_patterns_are_classified():
    """Every pattern ID in patterns.md must appear in the taxonomy.

    Orphan detection: if someone adds P018 without classifying it, this fails.
    """
    from agents.watcher.agent import load_pattern_severities

    sevs = load_pattern_severities()
    missing = [pid for pid in sevs if class_for_watcher_pattern(pid) is None]
    assert not missing, f"Watcher patterns not in taxonomy: {missing}"


def test_all_sentinel_findings_are_classified():
    """Every finding type Sentinel can emit must appear in the taxonomy."""
    # Authoritative list of finding types emitted by FleetState.analyze()
    sentinel_types = [
        "coordinated_degradation",
        "entropy_outlier",
        "verdict_shift",
        "correlated_events",
    ]
    missing = [ft for ft in sentinel_types if class_for_sentinel_finding(ft) is None]
    assert not missing, f"Sentinel findings not in taxonomy: {missing}"


def test_taxonomy_surfaces_exist_in_codebase():
    """Surface IDs in the YAML should reference real patterns/findings.

    Catches stale references (e.g. a removed pattern still listed).
    """
    from agents.watcher.agent import load_pattern_severities

    tax = load_taxonomy()
    watcher_patterns = set(load_pattern_severities().keys())

    for cls in tax["classes"]:
        for pid in cls["surfaces"].get("watcher_patterns", []):
            assert pid in watcher_patterns, (
                f"Taxonomy references {pid} in class {cls['id']} "
                f"but it's not in patterns.md"
            )


def test_coverage_summary(capsys):
    """Print coverage summary for CI output."""
    from agents.watcher.agent import load_pattern_severities

    tax = load_taxonomy()
    watcher_sevs = load_pattern_severities()
    watcher_mapped = sum(
        len(c["surfaces"].get("watcher_patterns", []))
        for c in tax["classes"]
    )
    sentinel_mapped = sum(
        len(c["surfaces"].get("sentinel_findings", []))
        for c in tax["classes"]
    )
    broadcast_mapped = sum(
        len(c["surfaces"].get("broadcast_events", []))
        for c in tax["classes"]
    )

    print(f"\nWatcher patterns covered: {watcher_mapped}/{len(watcher_sevs)}")
    print(f"Sentinel findings covered: {sentinel_mapped}")
    print(f"Broadcast events covered: {broadcast_mapped}")
