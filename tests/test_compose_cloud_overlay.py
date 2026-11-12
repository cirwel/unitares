"""docker-compose.cloud.yml stays a valid, hardened overlay of docker-compose.yml."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_CLOUD_ENV_VARS = (
    "UNITARES_HTTP_AGENT_TOKENS",
    "UNITARES_OAUTH_ISSUER_URL",
    "UNITARES_OAUTH_RESOURCE_URL",
    "UNITARES_OAUTH_SECRET",
    "UNITARES_OAUTH_PUBLIC_PORT",
    "UNITARES_OAUTH_REQUIRED",
    "UNITARES_OAUTH_DYNAMIC_REGISTRATION",
    "UNITARES_OAUTH_AUTO_APPROVE",
    "UNITARES_OAUTH_STATIC_CLIENT_ID",
    "UNITARES_OAUTH_STATIC_CLIENT_SECRET",
    "UNITARES_OAUTH_STATIC_REDIRECT_URIS",
)


def _load_compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_cloud_overlay_extends_governance_mcp_without_publishing_oauth_port():
    """bind_public_socket() binds 127.0.0.1 inside the container namespace, so
    publishing UNITARES_OAUTH_PUBLIC_PORT on a bridge port mapping would fail."""
    default = _load_compose("docker-compose.yml")["services"]
    cloud = _load_compose("docker-compose.cloud.yml")["services"]

    assert "governance-mcp" in default
    assert "governance-mcp" in cloud
    assert "ports" not in cloud["governance-mcp"], (
        "docker-compose.cloud.yml must not publish container-loopback "
        "UNITARES_OAUTH_PUBLIC_PORT on bridge ports"
    )

    env = cloud["governance-mcp"]["environment"]
    for name in EXPECTED_CLOUD_ENV_VARS:
        assert env.get(name) == f"${{{name}:-}}", (
            f"{name} must map as ${{{name}:-}} in docker-compose.cloud.yml"
        )


def test_cloudflared_profile_is_hardened_and_optional():
    cloud = _load_compose("docker-compose.cloud.yml")["services"]
    cf = cloud["cloudflared"]

    assert cf["profiles"] == ["cloudflare"]
    assert cf["network_mode"] == "service:governance-mcp"
    # network_mode: service:* forbids ports and networks on that container.
    assert "ports" not in cf
    assert "networks" not in cf
    assert cf["depends_on"] == {"governance-mcp": {"condition": "service_healthy"}}
    assert "no-new-privileges:true" in cf["security_opt"]
    assert cf["cap_drop"] == ["ALL"]
    assert cf["read_only"] is True
    assert ":latest" not in cf["image"]
    # Never use ${CLOUDFLARE_TUNNEL_TOKEN:?...}: Compose interpolates the whole
    # file even when the cloudflare profile is inactive, breaking Tailscale-only runs.
    non_comment_lines = [
        line
        for line in (ROOT / "docker-compose.cloud.yml").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert all(":?" not in line for line in non_comment_lines)
    assert cf["environment"]["TUNNEL_TOKEN"] == "${CLOUDFLARE_TUNNEL_TOKEN:-}"


def test_env_example_documents_cloud_vars_without_unsafe_oauth_defaults():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (*EXPECTED_CLOUD_ENV_VARS, "CLOUDFLARE_TUNNEL_TOKEN"):
        assert name in example, f".env.example omits {name}"

    # Never advertise open dynamic registration + auto-approve together.
    assert not re.search(
        r"^\s*#?\s*UNITARES_OAUTH_AUTO_APPROVE\s*=\s*(?:true|1|yes)\b",
        example,
        re.IGNORECASE | re.MULTILINE,
    )


def test_runbook_names_cloud_overlay_backup_script_and_ingress():
    runbook = (ROOT / "docs/operations/cloud-deploy-runbook.md").read_text(encoding="utf-8")
    assert "docker-compose.cloud.yml" in runbook
    assert "scripts/ops/backup_compose.sh" in runbook
    assert "http://127.0.0.1:8772" in runbook
    assert "http_status:404" in runbook


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI unavailable")
def test_docker_compose_config_interpolates_without_tunnel_token():
    probe = subprocess.run(
        ["docker", "compose", "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("docker compose plugin unavailable")

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k != "CLOUDFLARE_TUNNEL_TOKEN" and not k.startswith("UNITARES_OAUTH_")
    }
    res = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.cloud.yml",
            "config",
            "-q",
        ],
        cwd=ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
