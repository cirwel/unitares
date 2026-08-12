"""Version consistency guardrails."""

from scripts.ops.version_manager import (
    PROJECT_ROOT,
    VERSION_REFERENCES,
    check_file_versions,
    get_version,
)


def test_all_version_references_match_version_file():
    """All configured version references should match VERSION."""
    expected_version = get_version()
    mismatches = []

    for relative_path, patterns in VERSION_REFERENCES:
        mismatches.extend(
            check_file_versions(PROJECT_ROOT / relative_path, patterns, expected_version)
        )

    assert mismatches == [], f"Version mismatches found: {mismatches}"


def test_missing_configured_pattern_is_a_mismatch(tmp_path):
    """Stale release patterns must fail instead of silently checking nothing."""
    release_doc = tmp_path / "release.md"
    release_doc.write_text("release reference was reworded\n", encoding="utf-8")

    mismatches = check_file_versions(
        release_doc,
        [(r"server v([\d.]+)", r"server v{version}")],
        "2.18.0",
    )

    assert len(mismatches) == 1
    assert mismatches[0]["line"] == 0
    assert mismatches[0]["found"] == "<configured pattern did not match>"
