"""Startup checks for credentials left at a value anyone can read.

``docker-compose.yml`` gives three secrets a default so the quickstart runs
with no setup. Those defaults are published in this repository, so a server
still using one has a credential everyone already knows. The host ports in the
compose file are loopback-only, which is what makes that tolerable; the server
says so at startup so the operator learns it before rebinding a port, not after.

The defaults are held as SHA-256 digests rather than literals, so a rotation of
the compose defaults does not have one more copy to find.
``tests/test_insecure_defaults.py`` pins these digests to the compose file.

Stdlib only: this runs before bootstrap and must not import the web stack.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping

# Environment variable -> sha256 of the default docker-compose.yml assigns it.
PUBLISHED_DEFAULT_SHA256 = {
    "LEASE_PLANE_BEARER_TOKEN": "46378d16b298ff0b584fb7f7d9d11d74b36914788d9bf23c6426197bbff3322f",
    "UNITARES_CONTINUITY_TOKEN_SECRET": "973513065628b7d58160a920f829e640921bb86d5b761dc821bfa83dc3457e29",
    "UNITARES_LEASE_ATTESTATION_SIGNING_KEY": "ea866a757e4c38babfa8127cbe9a409d3e1f93a00ff1488ff735fcf917afffd0",
}


def published_default_secrets(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names of the secrets whose value is still the published compose default."""
    env = os.environ if environ is None else environ
    hits = []
    for name, digest in PUBLISHED_DEFAULT_SHA256.items():
        value = (env.get(name) or "").strip()
        if value and hashlib.sha256(value.encode()).hexdigest() == digest:
            hits.append(name)
    return hits


def running_in_container() -> bool:
    return os.path.exists("/.dockerenv")


def startup_warnings(
    *,
    rest_strict: bool,
    in_container: bool,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Operator-facing warnings about default or ineffective credentials."""
    env = os.environ if environ is None else environ
    warnings = []

    defaults = published_default_secrets(env)
    if defaults:
        warnings.append(
            f"{', '.join(defaults)} still set to the published docker-compose.yml "
            "default, which anyone can read. That is safe only while every host "
            "port stays bound to 127.0.0.1. Set your own values before exposing "
            "the server."
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
            "UNITARES_MCP_BEARER_TOKENS and leave UNITARES_REST_STRICT unset or set it to 1."
        )

    return warnings
