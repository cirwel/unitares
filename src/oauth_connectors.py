"""Durable, operator-managed OAuth clients; no deployment-specific roster.

Secrets are uniformly random 256-bit values, stored only as SHA-256 digests.
The SDK client authenticator is extended in oauth_connector_transport to check
these digests. Every lookup hits Postgres; revocation also denies cached tokens.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from urllib.parse import urlsplit

from src.db import get_db

CLIENT_PREFIX = "unitares_connector_"
CLIENT_ID_RE = re.compile(r"unitares_connector_[0-9a-f]{32}\Z")


def managed_client_id(client_id: str) -> bool:
    # Reserve the entire namespace, including malformed IDs: never fall back
    # to an env or dynamic registration for an unknown managed client.
    return client_id.startswith(CLIENT_PREFIX)


def secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def validate_registration(name, redirect_uris) -> tuple[str, list[str]]:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("Enter a connector name between 1 and 100 characters.")
    if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 8:
        raise ValueError("Paste 1–8 callback URLs supplied by your app.")
    result = []
    for uri in redirect_uris:
        if not isinstance(uri, str) or not uri or len(uri) > 2048:
            raise ValueError("Each callback must be a URL of at most 2048 characters.")
        if any(c.isspace() or ord(c) < 32 for c in uri) or any(c in uri for c in ("*", "\\", "#")):
            raise ValueError("Callback URLs cannot contain spaces, wildcards, backslashes or fragments.")
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Enter a valid callback URL.") from exc
        del port
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or not (parsed.scheme == "https" or (parsed.scheme == "http" and local))):
            raise ValueError("Use an HTTPS callback URL (HTTP is allowed only for loopback clients).")
        if uri not in result:
            result.append(uri)
    return name.strip(), result


def public_record(row) -> dict:
    data = dict(row)
    data.pop("secret_hash", None)
    if isinstance(data.get("redirect_uris"), str):
        data["redirect_uris"] = json.loads(data["redirect_uris"])
    for key in ("created_at", "revoked_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


class ConnectorRegistry:
    """Postgres is authoritative. Failures propagate; never admit in memory."""

    async def get(self, client_id: str) -> dict | None:
        if not CLIENT_ID_RE.fullmatch(client_id):
            return None
        async with asyncio.timeout(5), get_db().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM core.oauth_connectors WHERE client_id=$1 AND revoked_at IS NULL",
                client_id,
            )
        if row is None:
            return None
        result = dict(row)
        if isinstance(result["redirect_uris"], str):
            result["redirect_uris"] = json.loads(result["redirect_uris"])
        return result

    async def list(self) -> list[dict]:
        async with asyncio.timeout(5), get_db().acquire() as conn:
            rows = await conn.fetch(
                "SELECT client_id, name, redirect_uris, created_at, revoked_at "
                "FROM core.oauth_connectors ORDER BY created_at DESC LIMIT 200"
            )
        return [public_record(row) for row in rows]

    async def create(self, name, redirect_uris, actor: str) -> dict:
        name, redirect_uris = validate_registration(name, redirect_uris)
        client_id = CLIENT_PREFIX + secrets.token_hex(16)
        secret = secrets.token_urlsafe(32)
        async with asyncio.timeout(5), get_db().acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO core.oauth_connectors "
                "(client_id,name,secret_hash,redirect_uris,created_by) VALUES ($1,$2,$3,$4::jsonb,$5) "
                "RETURNING client_id,name,redirect_uris,created_at,revoked_at",
                client_id, name, secret_digest(secret), json.dumps(redirect_uris), actor,
            )
        # Only after the durable write succeeds. The raw secret never enters
        # a subsequent read, audit column, environment variable or log.
        return {**public_record(row), "client_secret": secret}

    async def revoke(self, client_id: str, actor: str) -> bool:
        if not CLIENT_ID_RE.fullmatch(client_id):
            return False
        async with asyncio.timeout(5), get_db().acquire() as conn:
            result = await conn.execute(
                "UPDATE core.oauth_connectors SET revoked_at=now(),revoked_by=$2 "
                "WHERE client_id=$1 AND revoked_at IS NULL", client_id, actor,
            )
        return result == "UPDATE 1"
