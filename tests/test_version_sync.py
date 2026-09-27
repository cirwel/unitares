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
    manual = tmp_path / "docs" / "manual" / "02-install.md"
    manual.parent.mkdir(parents=True)
    compatibility = tmp_path / "docs/COMPATIBILITY.md"
    source.write_text("2.21.0\n", encoding="utf-8")
    published.write_text("2.21.0\n", encoding="utf-8")
    manual.write_text(
        "git clone --branch v2.21.0 --depth 1 https://github.com/cirwel/unitares.git\n",
        encoding="utf-8",
    )
    historical = "Plugin bundle aligned with server `v2.21.0` at its tagged tree.\n"
    compatibility.write_text("| UNITARES server | `v2.21.0` |\n" + historical)
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(manager, "VERSION_FILE", source)
    monkeypatch.setattr(manager, "PUBLISHED_VERSION_FILE", published)
    monkeypatch.setattr("sys.argv", ["version_manager.py", "--update"])

    assert manager.bump_version("minor") == "2.22.0"
    manager.main()
    assert "git clone --branch v2.21.0 " in manual.read_text()
    assert published.read_text() == "2.21.0\n"
    assert historical in compatibility.read_text()
    assert "| UNITARES server | `v2.22.0` |" in compatibility.read_text()

    # RELEASE_PROCESS step 8 records verified publication independently.
    published.write_text("2.22.0\n", encoding="utf-8")
    manager.main()
    assert "git clone --branch v2.22.0 " in manual.read_text()
    assert source.read_text() == "2.22.0\n"


def test_publication_moves_the_compose_lease_plane_pin(tmp_path, monkeypatch):
    """Promote Release's pin job runs --update; Compose must follow the verified release."""
    import os
    import subprocess

    import yaml

    import scripts.ops.version_manager as manager

    source = tmp_path / "VERSION"
    published = tmp_path / "PUBLISHED_VERSION"
    compose = tmp_path / "docker-compose.yml"
    source.write_text("2.23.0\n", encoding="utf-8")
    published.write_text("2.23.0\n", encoding="utf-8")
    real_compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    current = (PROJECT_ROOT / "PUBLISHED_VERSION").read_text().strip()
    compose.write_text(real_compose.replace(f":v{current}", ":v2.22.99"))
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(manager, "VERSION_FILE", source)
    monkeypatch.setattr(manager, "PUBLISHED_VERSION_FILE", published)
    monkeypatch.setattr("sys.argv", ["version_manager.py", "--update"])

    manager.main()

    service = yaml.safe_load(compose.read_text())["services"]["lease-plane"]
    assert service["image"] == "ghcr.io/cirwel/unitares-lease-plane:v2.23.0"
    assert service["pull_policy"] == "missing"
    assert service["build"]["dockerfile"] == "elixir/lease_plane/Dockerfile"

    # The pin job refuses to commit unless this exact check passes afterwards.
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/promote-release.yml").read_text()
    )
    pin = next(
        step["run"]
        for step in workflow["jobs"]["pin"]["steps"]
        if step.get("name") == "Push a branch that advances PUBLISHED_VERSION"
    )
    check = next(line for line in pin.splitlines() if "grep -q" in line).strip()
    check = check.removesuffix("{").strip().removesuffix("||").strip()
    env = {
        **os.environ,
        "REGISTRY": "ghcr.io",
        "LEASE_PLANE_IMAGE_NAME": "cirwel/unitares-lease-plane",
        "RELEASE_TAG": "v2.23.0",
    }
    ok = subprocess.run(["bash", "-c", check], cwd=tmp_path, env=env, check=False)
    assert ok.returncode == 0
    stale = subprocess.run(
        ["bash", "-c", check],
        cwd=tmp_path,
        env={**env, "RELEASE_TAG": "v2.24.0"},
        check=False,
    )
    assert stale.returncode != 0
