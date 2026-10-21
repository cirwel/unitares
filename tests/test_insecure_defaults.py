"""Startup warnings for published compose secrets and an unchecked REST token."""

import hashlib
import re
from pathlib import Path

import pytest

from src.insecure_defaults import (
    ALLOW_PUBLISHED_DEFAULTS_ENV,
    PUBLISHED_DEFAULT_SHA256,
    exposure_signals,
    insecure_defaults_status,
    names_at_published_default,
    published_default_refusal,
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
    assert names_at_published_default(_published_env()) == list(PUBLISHED_DEFAULT_SHA256)


def test_own_values_unset_and_blank_are_not_reported():
    env = {name: "operator-chosen" for name in PUBLISHED_DEFAULT_SHA256}
    assert names_at_published_default(env) == []
    assert names_at_published_default({}) == []
    assert names_at_published_default({name: "  " for name in PUBLISHED_DEFAULT_SHA256}) == []


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
    # Strict auth also gates the in-stack clients, which send the singular bearer.
    assert "UNITARES_MCP_BEARER_TOKEN (" in warning
    assert "every other REST client" in warning

    assert startup_warnings(rest_strict=True, in_container=True, environ=env) == []
    assert startup_warnings(rest_strict=False, in_container=False, environ=env) == []
    assert startup_warnings(rest_strict=False, in_container=True, environ={}) == []


def test_clean_configuration_warns_about_nothing():
    env = {name: "operator-chosen" for name in PUBLISHED_DEFAULT_SHA256}
    assert startup_warnings(rest_strict=False, in_container=False, environ=env) == []


# --- Refusal when a published default meets an exposure signal -------------

def _compose_service_environment(service: str) -> dict[str, str]:
    """The environment docker compose gives `service` with no .env: defaults applied."""
    lines = COMPOSE.read_text().splitlines()
    start = lines.index(f"  {service}:")
    env: dict[str, str] = {}
    in_env = False
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line) or re.match(r"^\S", line):
            break
        if re.match(r"^    environment:\s*$", line):
            in_env = True
            continue
        if in_env and re.match(r"^    \S", line):
            in_env = False
        if not in_env:
            continue
        m = re.match(r"^      ([A-Z0-9_]+):\s*(.*)$", line)
        if not m:
            continue
        name, raw = m.group(1), m.group(2).strip().strip("'\"")
        env[name] = re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", raw)
    return env


QUICKSTART_ENV = _compose_service_environment("governance-mcp")


def _exposed(**extra: str) -> dict[str, str]:
    return {**_published_env(), **extra}


def test_quickstart_compose_environment_still_boots():
    # The plain quickstart keeps both published defaults and sets no exposure
    # signal, so it warns and serves, as before this guard existed.
    assert set(names_at_published_default(QUICKSTART_ENV)) == set(PUBLISHED_DEFAULT_SHA256)
    assert exposure_signals(QUICKSTART_ENV) == []
    assert published_default_refusal(QUICKSTART_ENV) is None
    assert startup_warnings(rest_strict=False, in_container=True, environ=QUICKSTART_ENV)


def test_quickstart_parser_saw_the_service_block():
    # Guards the parser above: an empty dict would pass the boot test vacuously.
    for name in ("UNITARES_MCP_ALLOWED_HOSTS", "UNITARES_MCP_ALLOWED_ORIGINS", *PUBLISHED_DEFAULT_SHA256):
        assert name in QUICKSTART_ENV, name


@pytest.mark.parametrize(
    "extra, signal",
    [
        ({"UNITARES_MCP_BEARER_TOKENS": "a,b"}, "UNITARES_MCP_BEARER_TOKENS"),
        ({"UNITARES_MCP_ALLOWED_HOSTS": "localhost,gov.example.org"}, "UNITARES_MCP_ALLOWED_HOSTS"),
        ({"UNITARES_MCP_ALLOWED_HOSTS": "192.168.1.20:8767"}, "UNITARES_MCP_ALLOWED_HOSTS"),
        ({"UNITARES_MCP_ALLOWED_HOSTS": "*"}, "UNITARES_MCP_ALLOWED_HOSTS"),
        ({"UNITARES_MCP_ALLOWED_ORIGINS": "https://gov.example.org"}, "UNITARES_MCP_ALLOWED_ORIGINS"),
        ({"UNITARES_DASHBOARD_RP_ID": "gov.example.org"}, "UNITARES_DASHBOARD_RP_ID"),
        ({"UNITARES_OAUTH_ISSUER_URL": "https://gov.example.org"}, "UNITARES_OAUTH_ISSUER_URL"),
        ({"UNITARES_MCP_ALLOWED_ORIGINS": "http://127.0.0.1.evil.example"}, "UNITARES_MCP_ALLOWED_ORIGINS"),
        ({"UNITARES_MCP_ALLOWED_HOSTS": "0.0.0.0"}, "UNITARES_MCP_ALLOWED_HOSTS"),
        # Unparseable: cannot be shown to be loopback, and must not crash startup.
        ({"UNITARES_MCP_ALLOWED_ORIGINS": "http://[::1"}, "UNITARES_MCP_ALLOWED_ORIGINS"),
    ],
)
def test_each_exposure_signal_refuses_with_a_published_default(extra, signal):
    env = _exposed(**extra)
    assert signal in exposure_signals(env)
    refusal = published_default_refusal(env)
    assert refusal is not None
    assert signal in refusal
    for name in PUBLISHED_DEFAULT_SHA256:
        assert name in refusal
    # Names only: no published value, and no configured value, is echoed.
    for value in (*_published_env().values(), *extra.values()):
        assert value not in refusal


@pytest.mark.parametrize(
    "extra",
    [
        {"UNITARES_MCP_ALLOWED_HOSTS": "localhost,127.0.0.1,[::1]:8767,127.0.0.1:*"},
        {"UNITARES_MCP_ALLOWED_ORIGINS": "http://localhost:8767,http://127.0.0.1:*,http://[::1]:8767,null"},
        {"UNITARES_DASHBOARD_RP_ID": "localhost"},
        {"UNITARES_DASHBOARD_ORIGIN": "https://gov.example.org"},  # ignored without an RP id
        {"UNITARES_OAUTH_ISSUER_URL": "http://127.0.0.1:8767"},
        {"UNITARES_MCP_BEARER_TOKENS": " , "},
    ],
)
def test_loopback_and_inert_configuration_is_not_exposure(extra):
    env = _exposed(**extra)
    assert exposure_signals(env) == []
    assert published_default_refusal(env) is None


def test_own_secrets_are_never_refused_whatever_the_exposure():
    env = {name: "operator-chosen" for name in PUBLISHED_DEFAULT_SHA256}
    env["UNITARES_MCP_BEARER_TOKENS"] = "a"
    env["UNITARES_OAUTH_ISSUER_URL"] = "https://gov.example.org"
    assert published_default_refusal(env) is None


@pytest.mark.parametrize(
    "origin", ["http://[::1", "http://[evil.example]", "https://[x"]
)
def test_malformed_urls_never_crash_and_never_read_as_loopback(origin):
    for name in ("UNITARES_MCP_ALLOWED_ORIGINS", "UNITARES_OAUTH_ISSUER_URL"):
        assert published_default_refusal(_exposed(**{name: origin})) is not None
        # With the operator's own secrets nothing about exposure is judged.
        own = {n: "operator-chosen" for n in PUBLISHED_DEFAULT_SHA256}
        assert published_default_refusal({**own, name: origin}) is None
        assert startup_warnings(rest_strict=False, in_container=False, environ={**own, name: origin}) == []


@pytest.mark.parametrize(
    "extra",
    [
        {"UNITARES_MCP_ALLOWED_ORIGINS": "HTTP://LOCALHOST:8767"},
        {"UNITARES_MCP_ALLOWED_ORIGINS": "http://user@127.0.0.1:8767"},
        {"UNITARES_MCP_ALLOWED_HOSTS": "::1"},
    ],
)
def test_loopback_spellings(extra):
    assert exposure_signals(_exposed(**extra)) == []


def test_userinfo_cannot_disguise_a_remote_host():
    env = _exposed(UNITARES_MCP_ALLOWED_ORIGINS="http://localhost@gov.example.org")
    assert exposure_signals(env) == ["UNITARES_MCP_ALLOWED_ORIGINS"]


def test_one_published_default_is_enough():
    name = next(iter(PUBLISHED_DEFAULT_SHA256))
    env = {name: _published_env()[name], "UNITARES_MCP_BEARER_TOKENS": "a"}
    refusal = published_default_refusal(env)
    assert refusal is not None and name in refusal


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_override_downgrades_the_refusal_to_a_loud_warning(value):
    env = _exposed(UNITARES_MCP_BEARER_TOKENS="a", **{ALLOW_PUBLISHED_DEFAULTS_ENV: value})
    assert published_default_refusal(env) is None
    warnings = startup_warnings(rest_strict=True, in_container=False, environ=env)
    override = [w for w in warnings if ALLOW_PUBLISHED_DEFAULTS_ENV in w]
    assert len(override) == 1
    assert "UNITARES_MCP_BEARER_TOKENS" in override[0]


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_override_needs_an_affirmative_value(value):
    env = _exposed(UNITARES_MCP_BEARER_TOKENS="a", **{ALLOW_PUBLISHED_DEFAULTS_ENV: value})
    assert published_default_refusal(env) is not None


def test_override_without_exposure_adds_no_warning():
    env = {**QUICKSTART_ENV, ALLOW_PUBLISHED_DEFAULTS_ENV: "1"}
    warnings = startup_warnings(rest_strict=False, in_container=False, environ=env)
    assert not any(ALLOW_PUBLISHED_DEFAULTS_ENV in w for w in warnings)


def test_health_status_names_defaults_and_signals_only():
    assert insecure_defaults_status({}) == {"status": "healthy", "insecure_defaults": []}

    local = insecure_defaults_status(QUICKSTART_ENV)
    assert local["status"] == "warning"
    assert set(local["insecure_defaults"]) == set(PUBLISHED_DEFAULT_SHA256)
    assert local["exposure_signals"] == []
    assert local["override"] is False

    demo = insecure_defaults_status(
        _exposed(UNITARES_MCP_BEARER_TOKENS="a", **{ALLOW_PUBLISHED_DEFAULTS_ENV: "1"})
    )
    assert demo["exposure_signals"] == ["UNITARES_MCP_BEARER_TOKENS"]
    assert demo["override"] is True
    rendered = repr(demo)
    for value in _published_env().values():
        assert value not in rendered


def test_refusal_is_judged_before_bootstrap():
    # Bootstrap SIGTERMs a running predecessor; a refusal after it would turn
    # a configuration mistake into an outage. Order in main(), by source.
    source = (Path(__file__).resolve().parents[1] / "src" / "mcp_server.py").read_text()
    refusal = source.index("_published_refusal = published_default_refusal()")
    assert source.index("raise SystemExit(1)", refusal) < source.index("bootstrap_server(", refusal)


def test_refusal_names_a_signing_key_recipe_the_server_can_load():
    # `openssl rand -hex 32` passed the startup check but failed at mint time:
    # 64 hex characters base64url-decode to 48 bytes, and the seed must be 32.
    import base64
    import os

    from src.insecure_defaults import SIGNING_KEY_COMMAND
    from src.lease_attestation import load_signing_key

    refusal = published_default_refusal(_exposed(UNITARES_MCP_BEARER_TOKENS="a"))
    assert SIGNING_KEY_COMMAND in refusal
    assert "openssl rand -hex 32" not in refusal.split("UNITARES_LEASE_ATTESTATION_SIGNING_KEY is")[1]
    # What the recipe produces: 32 random bytes, base64url, padding stripped.
    seed = base64.b64encode(os.urandom(32)).decode().translate(str.maketrans("+/", "-_")).rstrip("=")
    load_signing_key(seed)


def test_compose_passes_the_override_through():
    # Compose reads .env only to fill variables docker-compose.yml names, so an
    # override missing from the environment block could never be set there.
    assert QUICKSTART_ENV.get(ALLOW_PUBLISHED_DEFAULTS_ENV) == ""
