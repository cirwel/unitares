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
        assert "cryptography>=41.0.0,<51.0.0" in _read(requirements_file)


def _requirement_name(requirement: str) -> str:
    """'sentence-transformers>=2.2.0,<7.0.0' -> 'sentence-transformers'."""
    return re.split(r"[<>=!~\[;]", requirement, maxsplit=1)[0].strip().lower()


def _installed_names(relative: str) -> set[str]:
    """Package names a pip requirements file actually installs.

    Commented lines are deliberately excluded: both requirements-docker.txt and
    requirements-full.txt park an opt-in dependency as a commented re-enable
    line, and a commented line installs nothing.
    """
    names = set()
    for line in _read(relative).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-r ", "-c ", "--")):
            continue
        names.add(_requirement_name(line))
    return names


def test_full_extra_and_requirements_full_agree_on_what_is_installed() -> None:
    """pyproject's `full` extra and requirements-full.txt are hand-maintained
    separately, and nothing compared them as sets.

    That is how sentence-transformers came to sit in both while the container
    excluded it, and it is how a later edit to one file could silently miss the
    other: CI installs requirements-full.txt, the documented dev setup installs
    `.[full]`, so a divergence gives the two paths different dependency trees
    with no failing check anywhere.

    Directional on purpose. requirements-full.txt is a superset — it also
    carries the `dev` extra plus fakeredis, which is in no extra at all — so
    this asserts only that nothing in `full` is missing from it.
    """
    extras = tomllib.loads(_read("pyproject.toml"))["project"][
        "optional-dependencies"
    ]
    full = {_requirement_name(requirement) for requirement in extras["full"]}
    installed = _installed_names("requirements-full.txt")

    missing = sorted(full - installed)
    assert not missing, (
        f"in pyproject's `full` extra but not installed by "
        f"requirements-full.txt: {missing}"
    )


def test_optional_extras_are_not_installed_by_default() -> None:
    """A package in an opt-in extra must not arrive by default anyway.

    `embeddings` exists because sentence-transformers pulls torch and, on
    Linux, the whole CUDA wheel set with no GPU check (5.9 GB installed versus
    162 MB without). The extra only means something if the default install
    paths leave it out, so assert that rather than trusting the comment.
    """
    extras = tomllib.loads(_read("pyproject.toml"))["project"][
        "optional-dependencies"
    ]
    optional = {_requirement_name(r) for r in extras["embeddings"]}

    for requirements_file in ("requirements-full.txt", "requirements-docker.txt"):
        leaked = sorted(optional & _installed_names(requirements_file))
        assert not leaked, (
            f"{requirements_file} installs {leaked}, which the `embeddings` "
            f"extra exists to keep off the default path"
        )

    full = {_requirement_name(r) for r in extras["full"]}
    assert not (optional & full), (
        "the `embeddings` extra duplicates a package already in `full`, so "
        "`.[full]` would install it regardless"
    )


def test_dev_install_provides_starlette_testclient_backend() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    dev = project["optional-dependencies"]["dev"]

    assert any(requirement.startswith("httpx2>=2.0.0") for requirement in dev)
    assert "httpx2>=2.0.0,<3.0.0" in _read("requirements-full.txt")


def test_tier_one_install_is_release_pinned_and_single_command() -> None:
    readme = _read("README.md")
    manual = _read("docs/manual/02-install.md")

    # Source bumps must not advertise an unavailable release. Public examples
    # advance only after tag, release page, and container verification.
    # The README resolves that verified version at install time, so a release
    # never has to edit it; the manual keeps the explicit, bot-maintained pin.
    published = _read("PUBLISHED_VERSION").strip()
    pin = f"git clone --branch v{published} --depth 1"
    lookup = (
        'v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION)'
        ' && git clone --branch "v$v" --depth 1'
    )

    assert pin in manual
    install = readme.split("## Install\n", 1)[1].split("\n## ", 1)[0]
    assert install.count("```bash") == 1
    assert install.count(lookup) == 1
    assert re.search(r"--branch v\d", readme) is None
    assert "docker compose up -d --wait" in install
    assert "make demo" not in readme
    assert "make coordination-demo" not in readme


def test_operator_manual_keeps_coordination_validation_detail() -> None:
    manual = _read("docs/manual/02-install.md")
    compose = _read("docker-compose.yml")

    assert "make coordination-demo" in manual
    assert "one-command install/start" in manual
    assert "depends_on:" in compose
    assert "redis:" in compose
    assert "lease-plane:" in compose
    assert 'dockerfile: elixir/lease_plane/Dockerfile' in compose
    assert '"127.0.0.1:${LEASE_PLANE_HOST_PORT:-8788}:8788"' in compose
    assert "LEASE_PLANE_BASE_URL: http://lease-plane:8788" in compose
    assert "UNITARES_LEASE_PLANE_URL: http://lease-plane:8788" in compose
    assert "UNITARES_LEASE_IDENTITY_BINDING:" in compose
    assert "UNITARES_LEASE_IDENTITY_BOUND_SURFACE_KINDS:" in compose
    assert "UNITARES_LEASE_IDENTITY_PROOF_FORMAT:" in compose
    assert "UNITARES_LEASE_TRUSTED_ISSUERS:" in compose
    assert (
        "UNITARES_LEASE_IDENTITY_BOUND_SURFACE_KINDS: "
        "${UNITARES_LEASE_IDENTITY_BOUND_SURFACE_KINDS:-maintenance}"
    ) in compose
    assert len(
        re.findall(r"^      UNITARES_LEASE_ATTESTATION_AUDIENCE:", compose, re.MULTILINE)
    ) == 2
    assert "UNITARES_LEASE_TRUST_INSECURE_HTTP_URLS:" in compose
    assert "UNITARES_LEASE_TRUST_ALLOW_INSECURE_HTTP:" not in compose
    assert "UNITARES_LEASE_ATTESTATION_SIGNING_KEY:" in compose
    assert "refusing replay" in manual
    assert "UNITARES_CONTINUITY_TOKEN_SECRET:" in compose
    assert "UNITARES_MCP_BEARER_TOKENS:" in compose
    assert (
        "UNITARES_MCP_ALLOWED_HOSTS: "
        "${UNITARES_MCP_ALLOWED_HOSTS:-localhost,127.0.0.1}"
    ) in compose
    assert (
        "UNITARES_MCP_ALLOWED_ORIGINS: "
        "${UNITARES_MCP_ALLOWED_ORIGINS:-http://localhost:8767,http://127.0.0.1:8767}"
    ) in compose
    assert (
        "UNITARES_DASHBOARD_RP_ID: ${UNITARES_DASHBOARD_RP_ID:-gov.cirwel.org}"
    ) in compose
    assert "UNITARES_DASHBOARD_ORIGIN: ${UNITARES_DASHBOARD_ORIGIN:-}" in compose
    assert (
        "UNITARES_MCP_BEARER_TOKEN: ${UNITARES_MCP_BEARER_TOKEN:-}"
    ) in compose
    assert "UNITARES_REST_STRICT:" in compose
    assert "http://127.0.0.1:8767/health/ready" in compose
    assert "http://127.0.0.1:8767/v1/tools -o /dev/null" not in compose
    cloud_runbook = _read("docs/operations/cloud-session-plugin.md")
    assert "does **not** prove automatic hooks are active" in cloud_runbook
    assert "rejecting A's" in manual
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
