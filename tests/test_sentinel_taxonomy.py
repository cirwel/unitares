"""Cross-contract test: Sentinel's emitted finding types stay mapped in the
server-owned violation taxonomy (src/violation_taxonomy.yaml)."""


def test_findings_have_violation_class():
    """All finding types emitted by FleetState must have taxonomy mapping."""
    from src.violation_taxonomy import class_for_sentinel_finding

    sentinel_finding_types = [
        "coordinated_degradation",
        "entropy_outlier",
        "verdict_shift",
        "correlated_events",
    ]
    for ft in sentinel_finding_types:
        cls = class_for_sentinel_finding(ft)
        assert cls is not None, f"Sentinel finding type '{ft}' has no taxonomy mapping"
