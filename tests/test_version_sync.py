"""Version consistency guardrails."""

from scripts.ops.version_manager import (
    PROJECT_ROOT,
    check_file_versions,
    version_reference_groups,
)


def test_all_version_references_match_version_file():
    """Source claims and published installs match their respective authorities."""
    mismatches = []

    for expected_version, references in version_reference_groups():
        for relative_path, patterns in references:
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


def test_preparing_release_keeps_install_pins_until_publication(tmp_path, monkeypatch):
    """Exercise the release workflow: bump/update, then separately verify/publish."""
    import scripts.ops.version_manager as manager

    source = tmp_path / "VERSION"
    published = tmp_path / "PUBLISHED_VERSION"
    readme = tmp_path / "README.md"
    source.write_text("2.21.0\n", encoding="utf-8")
    published.write_text("2.21.0\n", encoding="utf-8")
    readme.write_text(
        "**Status:** v2.21.0.\n"
        "git clone --branch v2.21.0 --depth 1 https://github.com/cirwel/unitares.git\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(manager, "VERSION_FILE", source)
    monkeypatch.setattr(manager, "PUBLISHED_VERSION_FILE", published)
    monkeypatch.setattr("sys.argv", ["version_manager.py", "--update"])

    assert manager.bump_version("minor") == "2.22.0"
    manager.main()
    assert "**Status:** v2.22.0." in readme.read_text()
    assert "git clone --branch v2.21.0 " in readme.read_text()
    assert published.read_text() == "2.21.0\n"

    # RELEASE_PROCESS step 8 records verified publication independently.
    published.write_text("2.22.0\n", encoding="utf-8")
    manager.main()
    assert "git clone --branch v2.22.0 " in readme.read_text()
    assert source.read_text() == "2.22.0\n"
