"""Cross-file guardrails for the supported install and operator contract."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text()


def _current_version() -> str:
    return _read("VERSION").strip()


def test_shared_contract_http_setup_has_runnable_dependency_contract() -> None:
    install = (
        '3. Install dependencies: '
        '`pip install -e ".[full]" -c constraints.txt`'
    )
    start = '5. Start: `python src/mcp_server.py --port 8767`'

    for relative in ("AGENTS.md", "CLAUDE.md"):
        setup = _read(relative).split("## Setup\n", 1)[1].split("\n## ", 1)[0]
        assert install in setup
        assert start in setup

    full = tomllib.loads(_read("pyproject.toml"))["project"][
        "optional-dependencies"
    ]["full"]
    assert "uvicorn>=0.35.0,<1.0.0" in full
    assert any(requirement.startswith("starlette") for requirement in full)
    for requirements_file in ("requirements-full.txt", "requirements-docker.txt"):
        assert "uvicorn>=0.35.0,<1.0.0" in _read(requirements_file)


def test_dev_install_provides_starlette_testclient_backend() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    dev = project["optional-dependencies"]["dev"]

    assert any(requirement.startswith("httpx2>=2.0.0") for requirement in dev)
    assert "httpx2>=2.0.0,<3.0.0" in _read("requirements-full.txt")


def test_tier_one_quickstart_is_release_pinned_and_coordination_complete() -> None:
    readme = _read("README.md")
    manual = _read("docs/manual/02-install.md")
    compose = _read("docker-compose.yml")

    # Pinned to the release VERSION, not a frozen literal: version_manager
    # rewrites both files on every bump, so a literal here only ever reports
    # that a release happened. The contract being guarded is that the
    # quickstart names a release tag at all, never `master`.
    pin = f"git clone --branch v{_current_version()} --depth 1"

    assert "the supported install path" in readme
    assert pin in readme
    assert pin in manual
    assert "make coordination-demo" in readme
    assert "make coordination-demo" in manual
    assert "one-command install/start" in readme
    assert "one-command install/start" in manual
    assert "depends_on:" in compose
    assert "redis:" in compose
    assert "lease-plane:" in compose
    assert 'dockerfile: elixir/lease_plane/Dockerfile' in compose
    assert '"127.0.0.1:${LEASE_PLANE_HOST_PORT:-8788}:8788"' in compose
    assert "LEASE_PLANE_BASE_URL: http://lease-plane:8788" in compose
    assert "UNITARES_LEASE_PLANE_URL: http://lease-plane:8788" in compose
    assert "condition: service_healthy" in compose

    makefile = _read("Makefile")
    workflow = _read(".github/workflows/docker-quickstart.yml")
    assert "coordination-demo:" in makefile
    assert "scripts/demo/coordination_demo.py" in makefile
    assert "run: make coordination-demo" in workflow
    assert "elixir/lease_plane/**" in workflow


def test_advanced_bare_metal_path_uses_one_schema_bootstrap() -> None:
    playbook = _read("docs/install/PLAYBOOK.md")
    setup = _read("scripts/install/setup.py")

    command = "./scripts/install/bootstrap_postgres.sh --apply"
    assert command in playbook
    assert command in setup
    assert "psql -U postgres" not in setup
    assert "pip install unitares-core" not in playbook


def test_reader_recovery_never_claims_schema_auto_creation() -> None:
    troubleshooting = _read("docs/guides/TROUBLESHOOTING.md")
    assert "schema auto-creates" not in troubleshooting
    assert "not auto-create one" in troubleshooting
    assert "bootstrap_postgres.sh --apply" in troubleshooting
    assert "pg_restore --list" in troubleshooting
    assert troubleshooting.index("pg_restore --list") < troubleshooting.index(
        "DROP DATABASE IF EXISTS governance"
    )


def test_operator_surfaces_do_not_demote_redis_to_optional_cache() -> None:
    surfaces = [
        "docs/manual/06-operating.md",
        "docs/dev/DRIFT_LEDGER.md",
        "docs/operations/database_architecture.md",
        "requirements-full.txt",
        "src/services/runtime_queries.py",
    ]
    stale = ("Redis (optional)", "Redis optional cache", "session cache only")
    for relative in surfaces:
        content = _read(relative)
        for phrase in stale:
            assert phrase not in content, f"{relative} contains stale phrase {phrase!r}"


def _advertised_sdk_versions() -> dict[str, str]:
    """Every SDK version this repository advertises to the public, by surface."""
    surfaces = {
        "COMPATIBILITY.md": r"pip install unitares-sdk==([\d.]+)",
        "docs/public-site/index.md": r"pip install unitares-sdk==([\d.]+)",
    }
    found = {}
    for relative, pattern in surfaces.items():
        match = re.search(pattern, _read(relative))
        assert match, f"{relative} advertises no unitares-sdk install command"
        found[relative] = match.group(1)
    return found


def test_public_sdk_install_commands_agree() -> None:
    """Every public surface must name the same SDK version.

    This deliberately does NOT read agents/sdk/pyproject.toml. An earlier
    version of this test did, with the comment "the compatibility table
    advertises the version the repo *would* publish" — and would is not does.
    When #1800 bumped the declared version to 0.2.1, this test then *required*
    COMPATIBILITY.md to advertise `pip install unitares-sdk==0.2.1` while PyPI
    served 0.2.0, so a required test was holding a broken install command in
    place on a public page and reporting green.

    A public claim must be checked against the published artifact, never
    against repo intent. That check needs git tags, which this job's shallow
    checkout does not have, so it lives in scripts/ci/published_claims.py under
    the Release Seams workflow. What belongs here is the part that needs no
    network and no tags: the surfaces must not disagree with each other.
    """
    advertised = _advertised_sdk_versions()
    assert len(set(advertised.values())) == 1, (
        f"public surfaces advertise different SDK versions: {advertised}"
    )


def test_published_sdk_and_rest_envelope_are_current() -> None:
    compatibility = _read("COMPATIBILITY.md")
    manual = _read("docs/manual/03-running-the-server.md")
    assert "pip install unitares-sdk==" in compatibility
    assert "Until its first PyPI release" not in compatibility
    assert "-d '{\"name\":\"<tool_name>\"" in manual
    assert "-d '{\"tool\":\"<tool_name>\"" not in manual
