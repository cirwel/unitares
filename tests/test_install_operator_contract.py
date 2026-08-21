"""Cross-file guardrails for the supported install and operator contract."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text()


def test_tier_one_quickstart_is_release_pinned_and_redis_complete() -> None:
    readme = _read("README.md")
    manual = _read("docs/manual/02-install.md")
    compose = _read("docker-compose.yml")

    assert "the supported install path" in readme
    assert "git clone --branch v2.18.0 --depth 1" in readme
    assert "git clone --branch v2.18.0 --depth 1" in manual
    assert "depends_on:" in compose
    assert "redis:" in compose
    assert "condition: service_healthy" in compose


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


def test_published_sdk_and_rest_envelope_are_current() -> None:
    compatibility = _read("COMPATIBILITY.md")
    manual = _read("docs/manual/03-running-the-server.md")
    assert "pip install unitares-sdk==0.1.0" in compatibility
    assert "Until its first PyPI release" not in compatibility
    assert "-d '{\"name\":\"<tool_name>\"" in manual
    assert "-d '{\"tool\":\"<tool_name>\"" not in manual
