"""Startup warnings for published compose secrets and an unchecked REST token."""

import hashlib
import re
from pathlib import Path

from src.insecure_defaults import (
    PUBLISHED_DEFAULT_SHA256,
    published_default_secrets,
    startup_warnings,
)

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _compose_defaults() -> dict[str, set[str]]:
    """Every ``${NAME:-default}`` default in docker-compose.yml, by name."""
    found: dict[str, set[str]] = {}
    for name, default in re.findall(r"\$\{([A-Z0-9_]+):-([^}]*)\}", COMPOSE.read_text()):
        found.setdefault(name, set()).add(default)
    return found


def test_digests_match_the_compose_defaults():
    # A rotated compose default with a stale digest would silence the warning.
    defaults = _compose_defaults()
    for name, digest in PUBLISHED_DEFAULT_SHA256.items():
        assert name in defaults, name
        assert {hashlib.sha256(d.encode()).hexdigest() for d in defaults[name]} == {digest}, name


def _published_env() -> dict[str, str]:
    return {name: next(iter(values)) for name, values in _compose_defaults().items()
            if name in PUBLISHED_DEFAULT_SHA256}


def test_published_defaults_are_reported():
    assert published_default_secrets(_published_env()) == list(PUBLISHED_DEFAULT_SHA256)


def test_own_values_unset_and_blank_are_not_reported():
    env = {name: "operator-chosen" for name in PUBLISHED_DEFAULT_SHA256}
    assert published_default_secrets(env) == []
    assert published_default_secrets({}) == []
    assert published_default_secrets({name: "  " for name in PUBLISHED_DEFAULT_SHA256}) == []


def test_warning_names_each_default_secret():
    [warning] = startup_warnings(rest_strict=False, in_container=False, environ=_published_env())
    for name in PUBLISHED_DEFAULT_SHA256:
        assert name in warning
    assert "single-user local evaluation" in warning


def test_http_token_warning_only_in_a_container_in_local_posture():
    env = {"UNITARES_HTTP_API_TOKEN": "operator-chosen"}

    [warning] = startup_warnings(rest_strict=False, in_container=True, environ=env)
    assert "UNITARES_HTTP_API_TOKEN" in warning
    assert "trusted network addresses" in warning
    # The remediation must also undo an explicit UNITARES_REST_STRICT=0.
    assert "UNITARES_MCP_BEARER_TOKENS" in warning
    assert "UNITARES_REST_STRICT unset or set it to 1" in warning

    assert startup_warnings(rest_strict=True, in_container=True, environ=env) == []
    assert startup_warnings(rest_strict=False, in_container=False, environ=env) == []
    assert startup_warnings(rest_strict=False, in_container=True, environ={}) == []


def test_clean_configuration_warns_about_nothing():
    env = {name: "operator-chosen" for name in PUBLISHED_DEFAULT_SHA256}
    assert startup_warnings(rest_strict=False, in_container=False, environ=env) == []
