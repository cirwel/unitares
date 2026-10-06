"""The HMAC key that signs continuity tokens and effect grants.

A continuity token proves ownership of an agent: under strict identity it is
what lets a caller resume an agent by UUID. Whoever holds this key can mint one
for any agent, so the key must be private to this server.

Two sources, in order:

1. ``UNITARES_CONTINUITY_TOKEN_SECRET``, unless it is the value docker-compose.yml
   used to publish as a default (anyone can read that one).
2. A random key the server generates once at startup and keeps in
   ``data/secrets/continuity_token_secret`` (``UNITARES_CONTINUITY_TOKEN_SECRET_FILE``
   overrides the path), readable only by the server's user.

The HTTP API token is never used. It used to be the fallback, which made every
holder of that bearer able to forge ownership proofs.

Looking the key up only reads; :func:`ensure_generated_secret` creates the file
and runs at server startup. Deleting the file and restarting rotates the key,
which invalidates every outstanding token and grant.

Stdlib only: this runs before bootstrap and must not import the web stack.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path

ENV = "UNITARES_CONTINUITY_TOKEN_SECRET"
FILE_ENV = "UNITARES_CONTINUITY_TOKEN_SECRET_FILE"
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "secrets" / "continuity_token_secret"

# sha256 of the default docker-compose.yml assigned before this module existed.
RETIRED_COMPOSE_DEFAULT_SHA256 = "973513065628b7d58160a920f829e640921bb86d5b761dc821bfa83dc3457e29"


def is_retired_compose_default(value: str) -> bool:
    return hashlib.sha256(value.strip().encode()).hexdigest() == RETIRED_COMPOSE_DEFAULT_SHA256


def configured_secret(environ: Mapping[str, str] | None = None) -> str | None:
    """The operator's secret, or None when unset, blank or the published default.

    Returned byte for byte, surrounding whitespace included, so a deployment's
    key does not change across this upgrade.
    """
    value = (os.environ if environ is None else environ).get(ENV) or ""
    if not value.strip() or is_retired_compose_default(value):
        return None
    return value


def secret_file(environ: Mapping[str, str] | None = None) -> Path:
    """Where the generated secret lives (default data/secrets/continuity_token_secret)."""
    override = ((os.environ if environ is None else environ).get(FILE_ENV) or "").strip()
    return Path(override) if override else DEFAULT_FILE


def _read(path: Path) -> tuple[str | None, str | None]:
    """(key, problem) from the generated file.

    The file must be a regular file, not a symlink, owned by this process's
    user and closed to group and others: anyone who can read it can forge an
    ownership proof for any agent. Anything else is a problem, never a key.
    """
    try:
        # Nonblocking, so a FIFO at this path is refused below instead of
        # blocking startup until something writes to it.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot open {path}: {exc.strerror or exc}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, f"{path} is not a regular file"
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            return None, f"{path} is not owned by the server's user"
        if st.st_mode & 0o077:
            return None, f"{path} is readable by other users (mode {stat.S_IMODE(st.st_mode):o}); chmod 600 it"
        with os.fdopen(fd, encoding="utf-8", closefd=False) as fh:
            value = fh.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read {path}: {exc}"
    finally:
        os.close(fd)
    return (value, None) if value else (None, f"{path} is empty")


def resolve(environ: Mapping[str, str] | None = None) -> tuple[bytes, str] | None:
    """(key, source) for signing and verifying, or None when there is no key."""
    configured = configured_secret(environ)
    if configured:
        return configured.encode(), ENV
    generated, _problem = _read(secret_file(environ))
    if generated:
        return generated.encode(), "generated"
    return None


def ensure_generated_secret(environ: Mapping[str, str] | None = None) -> Path | None:
    """Create the generated key if no configured secret is in use.

    Returns the file now holding the key, or None when the operator configured
    one. Raises OSError when the file cannot be created or read back, or is
    open to other users, so a server that would otherwise run without
    ownership proofs says so.
    """
    if configured_secret(environ):
        return None
    path = secret_file(environ)
    value, problem = _read(path)
    if value:
        return path
    if problem is None:  # no file yet
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass  # another process created it first; read theirs below
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secrets.token_hex(32) + "\n")
        value, problem = _read(path)
    if not value:
        raise OSError(problem or f"continuity token secret file {path} is unusable")
    return path
