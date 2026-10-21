"""Startup checks for credentials left at a value anyone can read.

``docker-compose.yml`` gives two secrets a default so the quickstart runs
with no setup. Those defaults are published in this repository, so a server
still using one has a credential everyone already knows. That is tolerable only
for a single-user local evaluation (docs/DEPLOYMENT_SECURITY.md); the server
says so at startup so the operator learns it before sharing the host or
exposing a port, not after.

The defaults are held as SHA-256 digests rather than literals, so a rotation of
the compose defaults does not have one more copy to find.
``tests/test_insecure_defaults.py`` pins these digests to the compose file.

A published default together with configuration that says the server is
reached from beyond this host is refused at startup (``exposure_signals``,
``published_default_refusal``). Docker's host port mapping is invisible to the
process, so only configuration is judged: a plain local quickstart sets none
of these signals and keeps the warning alone. ``UNITARES_ALLOW_PUBLISHED_DEFAULTS=1``
downgrades the refusal to a warning for a deliberate demo.

Stdlib only: this runs before bootstrap and must not import the web stack.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

# Environment variable -> sha256 of the default docker-compose.yml assigns it.
PUBLISHED_DEFAULT_SHA256 = {
    "LEASE_PLANE_BEARER_TOKEN": "46378d16b298ff0b584fb7f7d9d11d74b36914788d9bf23c6426197bbff3322f",
    "UNITARES_LEASE_ATTESTATION_SIGNING_KEY": "ea866a757e4c38babfa8127cbe9a409d3e1f93a00ff1488ff735fcf917afffd0",
}


def names_at_published_default(environ: Mapping[str, str] | None = None) -> list[str]:
    """Variable names (never values) still set to their published compose default."""
    env = os.environ if environ is None else environ
    hits = []
    for name, digest in PUBLISHED_DEFAULT_SHA256.items():
        value = (env.get(name) or "").strip()
        if value and hashlib.sha256(value.encode()).hexdigest() == digest:
            hits.append(name)
    return hits


def running_in_container() -> bool:
    return os.path.exists("/.dockerenv")


ALLOW_PUBLISHED_DEFAULTS_ENV = "UNITARES_ALLOW_PUBLISHED_DEFAULTS"

# A fresh signing seed in the format src/lease_attestation.py loads: 32 random
# bytes, base64url without padding. `openssl rand -hex 32` would give 64 hex
# characters, which base64url-decode to 48 bytes and are refused at mint time.
SIGNING_KEY_COMMAND = "openssl rand 32 | base64 | tr '+/' '-_' | tr -d '='"


def _csv(env: Mapping[str, str], name: str) -> list[str]:
    return [item.strip() for item in (env.get(name) or "").split(",") if item.strip()]


def _is_loopback_host(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _allowed_host_entry_is_loopback(entry: str) -> bool:
    """An UNITARES_MCP_ALLOWED_HOSTS entry: ``host``, ``host:port`` or ``[v6]:port``."""
    if entry.startswith("["):
        return _is_loopback_host(entry[1:].split("]", 1)[0])
    if entry.count(":") == 1:
        entry = entry.split(":", 1)[0]
    return _is_loopback_host(entry)


def _url_host_is_loopback(url: str) -> bool:
    # A port given as "*" (as in the built-in origin allowlist) is not a valid
    # port, so read the host from the netloc instead of urlsplit().hostname's
    # port parsing, which would raise. A value urlsplit cannot parse at all
    # (an unclosed IPv6 bracket) cannot be shown to be loopback, so it counts
    # as exposure rather than crashing startup.
    try:
        netloc = urlsplit(url if "//" in url else f"//{url}").netloc
    except ValueError:
        return False
    return _allowed_host_entry_is_loopback(netloc.rsplit("@", 1)[-1])


def exposure_signals(environ: Mapping[str, str] | None = None) -> list[str]:
    """Variable names whose values say clients reach this server from beyond this host.

    Each one is a configuration statement the server already acts on:

    - ``UNITARES_MCP_BEARER_TOKENS``: any member turns on the hosted ``/mcp``
      bearer gate and strict REST (src/mcp_listen_config.py).
    - ``UNITARES_MCP_ALLOWED_HOSTS`` / ``UNITARES_MCP_ALLOWED_ORIGINS``: an entry
      naming a host other than loopback admits ``/mcp`` requests addressed to it.
      The opaque ``null`` origin is not a host and is ignored.
    - ``UNITARES_DASHBOARD_RP_ID`` / ``UNITARES_DASHBOARD_ORIGIN``: a passkey
      relying party on a host other than loopback. The origin counts only with
      an RP id, because src/dashboard_auth.py ignores it otherwise.
    - ``UNITARES_OAUTH_ISSUER_URL`` on a host other than loopback: OAuth is
      configured for remote connectors. The public OAuth listener
      (``UNITARES_OAUTH_PUBLIC_PORT``) needs an issuer, so this covers it.

    The bind address is not a signal: inside the compose container the server
    binds every interface by design and only the host port mapping, which the
    process cannot see, decides reachability.
    """
    env = os.environ if environ is None else environ
    signals = []
    if _csv(env, "UNITARES_MCP_BEARER_TOKENS"):
        signals.append("UNITARES_MCP_BEARER_TOKENS")
    if any(not _allowed_host_entry_is_loopback(h) for h in _csv(env, "UNITARES_MCP_ALLOWED_HOSTS")):
        signals.append("UNITARES_MCP_ALLOWED_HOSTS")
    origins = [o for o in _csv(env, "UNITARES_MCP_ALLOWED_ORIGINS") if o.lower() != "null"]
    if any(not _url_host_is_loopback(o) for o in origins):
        signals.append("UNITARES_MCP_ALLOWED_ORIGINS")
    rp_id = (env.get("UNITARES_DASHBOARD_RP_ID") or "").strip()
    if rp_id:
        if not _is_loopback_host(rp_id):
            signals.append("UNITARES_DASHBOARD_RP_ID")
        origin = (env.get("UNITARES_DASHBOARD_ORIGIN") or "").strip()
        if origin and not _url_host_is_loopback(origin):
            signals.append("UNITARES_DASHBOARD_ORIGIN")
    issuer = (env.get("UNITARES_OAUTH_ISSUER_URL") or "").strip()
    if issuer and not _url_host_is_loopback(issuer):
        signals.append("UNITARES_OAUTH_ISSUER_URL")
    return signals


def published_defaults_allowed(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(ALLOW_PUBLISHED_DEFAULTS_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def published_default_refusal(environ: Mapping[str, str] | None = None) -> str | None:
    """The startup refusal, or None to serve.

    Refuses only when a published default is in use AND configuration says the
    server is reached from beyond this host, and the operator has not set the
    override. Names variables, never values.
    """
    env = os.environ if environ is None else environ
    defaults = names_at_published_default(env)
    if not defaults:
        return None
    signals = exposure_signals(env)
    if not signals or published_defaults_allowed(env):
        return None
    return (
        f"Refusing to start: {', '.join(defaults)} still set to the published "
        "docker-compose.yml default, which anyone can read, while "
        f"{', '.join(signals)} configure this server for clients beyond this host. "
        "Set your own values first, in the server's process environment (the "
        "governance-mcp environment block or .env for docker compose, the plist on "
        "a launchd install). LEASE_PLANE_BEARER_TOKEN takes any long random string "
        "(for example `openssl rand -hex 32`) and must match on the lease plane, so "
        "set it in both services. UNITARES_LEASE_ATTESTATION_SIGNING_KEY is a "
        "base64url-encoded 32-byte Ed25519 seed: "
        f"`{SIGNING_KEY_COMMAND}`. For a deliberate demo, "
        f"{ALLOW_PUBLISHED_DEFAULTS_ENV}=1 downgrades this refusal to a warning "
        "(docs/DEPLOYMENT_SECURITY.md)."
    )


def insecure_defaults_status(environ: Mapping[str, str] | None = None) -> dict:
    """The health-check entry: which published defaults are in use, by name only."""
    env = os.environ if environ is None else environ
    defaults = names_at_published_default(env)
    if not defaults:
        return {"status": "healthy", "insecure_defaults": []}
    signals = exposure_signals(env)
    entry = {
        "status": "warning",
        "insecure_defaults": defaults,
        "exposure_signals": signals,
        "override": bool(signals) and published_defaults_allowed(env),
        "warning": (
            f"{', '.join(defaults)} still set to the published docker-compose.yml "
            "default, which anyone can read. Set your own values "
            "(docs/DEPLOYMENT_SECURITY.md)."
        ),
    }
    return entry


def startup_warnings(
    *,
    rest_strict: bool,
    in_container: bool,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Operator-facing warnings about default or ineffective credentials."""
    env = os.environ if environ is None else environ
    warnings = []

    defaults = names_at_published_default(env)
    if defaults:
        warnings.append(
            f"{', '.join(defaults)} still set to the published docker-compose.yml "
            "default, which anyone can read. That suits only a single-user local "
            "evaluation: any process on this host can reach the loopback ports, and "
            "on native Linux the Compose bridge too (docs/DEPLOYMENT_SECURITY.md). "
            "Set your own values before sharing the host or exposing the server."
        )
        signals = exposure_signals(env)
        if signals and published_defaults_allowed(env):
            warnings.append(
                f"{ALLOW_PUBLISHED_DEFAULTS_ENV} is overriding a startup refusal: "
                f"{', '.join(defaults)} are published defaults and "
                f"{', '.join(signals)} configure this server for clients beyond this "
                "host. Anyone who can reach it can use those credentials. Unset "
                f"{ALLOW_PUBLISHED_DEFAULTS_ENV} once the demo is over."
            )

    # docker-compose.yml used to give the continuity secret a published
    # default. The server now ignores that value and signs with a generated
    # key (src/continuity_secret.py); say so to anyone whose .env still has it.
    from src.continuity_secret import ENV as CONTINUITY_ENV, is_retired_compose_default

    if is_retired_compose_default(env.get(CONTINUITY_ENV) or ""):
        warnings.append(
            f"{CONTINUITY_ENV} is set to the default docker-compose.yml used to "
            "publish. The server ignores that value and signs continuity tokens with "
            "a generated secret in data/secrets/. Remove it from your environment, or "
            "set your own value."
        )

    # Local REST posture admits a caller from a trusted network address
    # (loopback, private ranges) before it looks at UNITARES_HTTP_API_TOKEN
    # (src/http_routes/access.py). A container on the Docker bridge sees its
    # callers from the bridge gateway, which is such an address, so there the
    # token usually admits no one. Callers from untrusted addresses, and the
    # OAuth public listener, are still checked.
    if in_container and (env.get("UNITARES_HTTP_API_TOKEN") or "").strip() and not rest_strict:
        warnings.append(
            "UNITARES_HTTP_API_TOKEN is set, but REST is in local posture, which admits "
            "callers from trusted network addresses (loopback and private ranges) "
            "without checking it. Behind the Docker bridge, callers arrive from such "
            "an address. To require a credential on every REST call, set "
            "UNITARES_MCP_BEARER_TOKENS, set UNITARES_MCP_BEARER_TOKEN (the credential "
            "the lease plane and orchestrated reviewers send) to one of its members, "
            "give every other REST client a member too (strict posture does not accept "
            "UNITARES_HTTP_API_TOKEN), and leave UNITARES_REST_STRICT unset or set it to 1."
        )

    return warnings
