"""Tests for scripts/ops/apply_dotenv_overlay.py and cloud overlay env pass-through."""

from __future__ import annotations

import hashlib
import importlib.util
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/ops/apply_dotenv_overlay.py"
COMPOSE_BASE = REPO / "docker-compose.yml"
COMPOSE_CLOUD = REPO / "docker-compose.cloud.yml"

_spec = importlib.util.spec_from_file_location("apply_dotenv_overlay", SCRIPT)
assert _spec is not None and _spec.loader is not None
dotenv_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dotenv_mod)


def _dotenv(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _overlay(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "governance-mcp.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_updates_existing_appends_missing_and_preserves_comments_and_secrets(tmp_path: Path):
    dotenv = _dotenv(
        tmp_path,
        "# Top comment\n"
        "POSTGRES_PASSWORD=super-secret-db-pass\n"
        "UNITARES_TRUSTED_NETWORKS=192.168.1.0/24\n"
        "\n"
        "# Footer comment\n"
        "UNITARES_MCP_BEARER_TOKENS=tok-a,tok-b\n",
        mode=0o644,  # tightened to 0600 on write
    )
    overlay = _overlay(
        tmp_path,
        "# Reviewed overlay\n"
        "UNITARES_TRUSTED_NETWORKS=100.64.0.0/10\n"
        "UNITARES_RESIDENTS=Alpha,Beta\n",
    )

    changed = dotenv_mod.apply_overlay(dotenv, overlay)
    assert changed == ["UNITARES_TRUSTED_NETWORKS", "UNITARES_RESIDENTS"]
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600

    rendered = dotenv.read_text(encoding="utf-8")
    assert rendered == (
        "# Top comment\n"
        "POSTGRES_PASSWORD=super-secret-db-pass\n"
        "UNITARES_TRUSTED_NETWORKS=100.64.0.0/10\n"
        "\n"
        "# Footer comment\n"
        "UNITARES_MCP_BEARER_TOKENS=tok-a,tok-b\n"
        "\n"
        "UNITARES_RESIDENTS=Alpha,Beta\n"
    )


def test_applied_overlay_is_byte_for_byte_noop(tmp_path: Path):
    original = (
        "# Hand-formatted .env\n\n"
        "POSTGRES_PASSWORD=secret\n"
        "A_KEY=v\n"
        "QUOTED_INLINE='bar' # operator note\n"
        "JSON_KEY='{\"a\":\"b\"}'\n"
    )
    dotenv = _dotenv(tmp_path, original)
    overlay = _overlay(tmp_path, 'A_KEY=v\nQUOTED_INLINE=bar\nJSON_KEY={"a":"b"}\n')
    before = dotenv.read_bytes()
    assert dotenv_mod.apply_overlay(dotenv, overlay) == []
    assert dotenv.read_bytes() == before


def test_unterminated_quotes_and_root_rewrite_prefix_and_mode_tightening(tmp_path: Path):
    # 1. Unterminated quote in .env fails closed without echoing value.
    bad_dotenv = _dotenv(tmp_path, "BROKEN='unterminated-secret\n")
    with pytest.raises(dotenv_mod.OverlayError, match="unterminated"):
        dotenv_mod.apply_overlay(bad_dotenv, _overlay(tmp_path, "A_KEY=v\n"))

    # 2. --rewrite-prefix FROM=/ never produces double leading slashes and sorts longest-first.
    rewrites = dotenv_mod.parse_rewrite_prefixes(["/host=/", "/host/sub=/opt/sub"])
    transformed = dotenv_mod.transform_overlay(
        {"P1": "/host/etc/cfg.json", "P2": "/host/sub/a.json"},
        rewrite_prefixes=rewrites,
    )
    assert transformed == {"P1": "/etc/cfg.json", "P2": "/opt/sub/a.json"}

    # 3. Permissive 0644 .env with already-matching keys is tightened to 0600 (and --check exits 3).
    permissive = _dotenv(tmp_path, "A_KEY=v\n", mode=0o644)
    ov = _overlay(tmp_path, "A_KEY=v\n")
    chk = subprocess.run(
        [sys.executable, str(SCRIPT), "--dotenv", str(permissive), "--overlay", str(ov), "--check"],
        capture_output=True,
        text=True,
    )
    assert chk.returncode == 3
    assert stat.S_IMODE(permissive.stat().st_mode) == 0o644
    assert dotenv_mod.apply_overlay(permissive, ov) == []
    assert stat.S_IMODE(permissive.stat().st_mode) == 0o600


def test_single_quotes_values_with_dollar_hash_or_spaces_and_refuses_single_quote(tmp_path: Path):
    dotenv = _dotenv(tmp_path, "POSTGRES_PASSWORD=secret\n")
    overlay = _overlay(
        tmp_path,
        'JSON_ISSUERS={"op":"https://a.example/keys"}\n'
        "DOLLAR_VAL=prefix$literal\n",
    )
    assert dotenv_mod.apply_overlay(dotenv, overlay) == ["JSON_ISSUERS", "DOLLAR_VAL"]
    text = dotenv.read_text(encoding="utf-8")
    assert """JSON_ISSUERS='{"op":"https://a.example/keys"}'""" in text
    assert "DOLLAR_VAL='prefix$literal'" in text
    # Second pass recognizes the single-quoted values as already applied
    assert dotenv_mod.apply_overlay(dotenv, overlay) == []

    bad_overlay = _overlay(tmp_path, "BAD_VAL=has'quote\n")
    with pytest.raises(dotenv_mod.OverlayError, match="single quote"):
        dotenv_mod.apply_overlay(dotenv, bad_overlay)


def test_rewrite_prefix_and_omit_key_operate_after_sha256_verification(tmp_path: Path):
    dotenv = _dotenv(tmp_path, "POSTGRES_PASSWORD=secret\n")
    overlay_text = (
        "UNITARES_ROUTE_PACKS_FILE=/host/fleet/config/route-packs.json\n"
        "UNITARES_METRICS_CATALOG_EXTRA=/host/fleet/config2/other.json\n"
        "UNITARES_HUB_TOKEN=sha256:" + ("a" * 64) + "\n"
    )
    overlay = _overlay(tmp_path, overlay_text)
    digest = hashlib.sha256(overlay_text.encode("utf-8")).hexdigest()

    rewrites = dotenv_mod.parse_rewrite_prefixes(["/host/fleet/config=/etc/unitares/config"])
    omits = dotenv_mod.parse_omit_keys(["UNITARES_HUB_TOKEN"])
    changed = dotenv_mod.apply_overlay(
        dotenv,
        overlay,
        expected_sha256=digest,
        rewrite_prefixes=rewrites,
        omit_keys=omits,
    )
    assert changed == ["UNITARES_ROUTE_PACKS_FILE", "UNITARES_METRICS_CATALOG_EXTRA"]
    rendered = dotenv.read_text(encoding="utf-8")
    assert "UNITARES_ROUTE_PACKS_FILE=/etc/unitares/config/route-packs.json\n" in rendered
    # Path boundary preserved: /host/fleet/config2 must NOT match /host/fleet/config
    assert "UNITARES_METRICS_CATALOG_EXTRA=/host/fleet/config2/other.json\n" in rendered
    assert "UNITARES_HUB_TOKEN" not in rendered

    with pytest.raises(dotenv_mod.OverlayError, match="SHA-256"):
        dotenv_mod.apply_overlay(dotenv, overlay, expected_sha256="0" * 64)


def test_symlinked_dotenv_stays_symlink_and_writes_once_backup(tmp_path: Path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_dotenv = _dotenv(real_dir, "# original comment\nA_KEY=old\n")
    link = tmp_path / ".env"
    link.symlink_to(real_dotenv)
    backups = tmp_path / "backups"

    dotenv_mod.apply_overlay(link, _overlay(tmp_path, "A_KEY=first\n"), backup_dir=backups)
    assert link.is_symlink()
    backup_file = backups / ".env.pre-overlay"
    assert backup_file.read_text(encoding="utf-8") == "# original comment\nA_KEY=old\n"
    assert stat.S_IMODE(backup_file.stat().st_mode) == 0o600

    # Second change preserves the first pre-overlay backup
    dotenv_mod.apply_overlay(link, _overlay(tmp_path, "A_KEY=second\n"), backup_dir=backups)
    assert backup_file.read_text(encoding="utf-8") == "# original comment\nA_KEY=old\n"
    assert "A_KEY=second\n" in real_dotenv.read_text(encoding="utf-8")


def test_refuses_duplicate_keys_or_export_prefix_without_leaking_values(tmp_path: Path):
    dup = _dotenv(tmp_path, "SECRET_KEY=top-secret-value\nSECRET_KEY=other-secret\n")
    overlay = _overlay(tmp_path, "A_KEY=v\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dotenv", str(dup), "--overlay", str(overlay)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "set more than once" in result.stderr
    assert "top-secret-value" not in result.stderr
    assert "other-secret" not in result.stderr

    exp = _dotenv(tmp_path, "export SECRET_KEY=top-secret-value\n")
    result_exp = subprocess.run(
        [sys.executable, str(SCRIPT), "--dotenv", str(exp), "--overlay", str(overlay)],
        capture_output=True,
        text=True,
    )
    assert result_exp.returncode == 2
    assert "top-secret-value" not in result_exp.stderr


def test_cli_validate_and_check_exit_codes(tmp_path: Path):
    dotenv = _dotenv(tmp_path, "A_KEY=old\n")
    overlay_text = "A_KEY=new-secret-value\n"
    overlay = _overlay(tmp_path, overlay_text)
    digest = hashlib.sha256(overlay_text.encode("utf-8")).hexdigest()

    val = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--overlay",
            str(overlay),
            "--expected-sha256",
            digest,
            "--validate-overlay",
        ],
        capture_output=True,
        text=True,
    )
    assert val.returncode == 0
    assert "overlay validated" in val.stdout

    chk = subprocess.run(
        [sys.executable, str(SCRIPT), "--dotenv", str(dotenv), "--overlay", str(overlay), "--check"],
        capture_output=True,
        text=True,
    )
    assert chk.returncode == 3
    assert "would set in .env: A_KEY" in chk.stdout
    assert "new-secret-value" not in chk.stdout
    assert dotenv.read_text(encoding="utf-8") == "A_KEY=old\n"


def test_compose_stack_forwards_operator_overlay_keys_into_governance_mcp():
    base_cfg = yaml.safe_load(COMPOSE_BASE.read_text(encoding="utf-8"))
    cloud_cfg = yaml.safe_load(COMPOSE_CLOUD.read_text(encoding="utf-8"))
    combined_env = {
        **base_cfg["services"]["governance-mcp"]["environment"],
        **cloud_cfg["services"]["governance-mcp"]["environment"],
    }
    expected_keys = {
        "UNITARES_TRUSTED_NETWORKS",
        "UNITARES_TRUSTED_SUBSTRATE_MARKERS",
        "UNITARES_CREDENTIAL_ISSUANCE",
        "UNITARES_LEGACY_SESSION_IDS",
        "UNITARES_RESIDENTS",
        "UNITARES_RESIDENT_PROGRESS_MANIFEST",
        "UNITARES_ROUTE_PACKS",
        "UNITARES_ROUTE_PACKS_FILE",
        "UNITARES_METRICS_CATALOG_EXTRA",
        "UNITARES_INFERENCE_HOSTS_FILE",
        "UNITARES_HTTP_AGENT_TOKENS",
        "UNITARES_HUB_CALLBACK_URL",
        "UNITARES_HUB_TOKEN",
        "UNITARES_READ_ONLY_TOKENS",
    }
    missing = expected_keys - set(combined_env)
    assert not missing, f"governance-mcp compose environment missing overlay keys: {sorted(missing)}"
    volumes = cloud_cfg["services"]["governance-mcp"]["volumes"]
    assert "./agents:/app/agents:ro" in volumes
    assert "./scripts/ops:/app/scripts/ops:ro" in volumes
