"""
Server-side canonicalization helper for surface_id (RFC v0.8 §7.12).

Per-scheme normalization rules — the single point of truth for split-brain
prevention. Two callers using different paths to the same logical surface
must produce the same canonical surface_id IFF both go through this helper.

Authority is server-side: the lease plane re-canonicalizes on receipt against
its own filesystem semantics (RFC §7.12.1, dialectic option (i)).

Three verifier findings drove the implementation details below:

  - DRIFT-2 (/var → /private/var): os.path.realpath on macOS does NOT
    idempotently re-resolve. /var/folders/.../tmpfile and
    /private/var/folders/.../tmpfile would otherwise produce two distinct
    canonical strings. Helper double-applies realpath.

  - DRIFT-3 (pathconf REFUTED): os.pathconf_names on macOS Python does NOT
    contain 'PC_CASE_SENSITIVE'. The RFC's original probe spec was wrong;
    helper uses a tmpfile probe instead (write 'PROBE', stat 'probe').

  - Canonical scheme list (RFC §7.2.1 plus follow-on presence/maintenance
    schemes); helper dispatches per-scheme.
"""

from __future__ import annotations

import os
import os.path
import tempfile

# v0.8 canonical scheme list (RFC §7.2.1) plus follow-on schemes. Single source
# of truth in code. `agent` added by migration 042; `maintenance` by migration 050.
CANONICAL_SCHEMES: tuple[str, ...] = (
    "file",
    "dialectic",
    "resident",
    "maintenance",
    "capture",
    "td",
    "agent",
)

# Compiled scheme-grammar regex matches migrations 026 + 042 + 049 at the DB layer.
_SCHEME_GRAMMAR = "^(file://|dialectic:/|resident:/|maintenance:/|capture:/|td:/|agent:/)"

_PATH_MAX = 4096

_case_insensitive_cache: bool | None = None


class CanonicalizeError(Exception):
    """Raised when a surface_id cannot be canonicalized.

    `reason` is one of:
      - 'symlink_loop' — realpath hit ELOOP
      - 'path_too_long' — path exceeds PATH_MAX
      - 'invalid_scheme' — surface_id doesn't match the canonical scheme list
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _detect_case_insensitive(probe_root: str | None = None) -> bool:
    """Tmpfile probe: write 'PROBE', stat 'probe', return whether they're the same file.

    DRIFT-3 mitigation: do NOT use pathconf(_PC_CASE_SENSITIVE) — REFUTED on macOS Python
    (PC_CASE_SENSITIVE absent from os.pathconf_names; calling raises ValueError).

    Args:
        probe_root: Optional directory to probe. None uses tempfile.gettempdir().
                    Tests can override; production uses the server's local FS.
    """
    with tempfile.TemporaryDirectory(prefix="lease_canonicalize_probe_", dir=probe_root) as d:
        upper = os.path.join(d, "PROBE")
        lower = os.path.join(d, "probe")
        with open(upper, "w") as f:
            f.write("")
        return os.path.exists(lower)


def _is_case_insensitive() -> bool:
    """Cached result of the tmpfile probe; runs once per process."""
    global _case_insensitive_cache
    if _case_insensitive_cache is None:
        _case_insensitive_cache = _detect_case_insensitive()
    return _case_insensitive_cache


def canonicalize(surface_id: str) -> str:
    """Return the canonical form of a surface_id per RFC v0.8 §7.12.1.

    Raises:
        CanonicalizeError: symlink loop, path too long, or invalid scheme.
        ValueError: NUL byte in input (caller-side reject — matches stdlib).
    """
    if "\x00" in surface_id:
        raise ValueError("NUL byte in surface_id")
    if len(surface_id) > _PATH_MAX:
        raise CanonicalizeError("path_too_long", f"surface_id length {len(surface_id)} > PATH_MAX {_PATH_MAX}")

    if surface_id.startswith("file://"):
        return _canonicalize_file(surface_id[len("file://") :])
    if surface_id.startswith("dialectic:/"):
        return _canonicalize_dialectic(surface_id[len("dialectic:/") :])
    if surface_id.startswith("resident:/"):
        return _canonicalize_resident(surface_id[len("resident:/") :])
    if surface_id.startswith("maintenance:/"):
        return _canonicalize_maintenance(surface_id[len("maintenance:/") :])
    if surface_id.startswith("capture:/"):
        return _canonicalize_capture(surface_id[len("capture:/") :])
    if surface_id.startswith("td:/"):
        # Reserved scheme; pass-through with no normalization beyond what the
        # caller supplied. Field_validator ensures the prefix matched the grammar.
        return f"td:/{surface_id[len('td:/') :]}"
    if surface_id.startswith("agent:/"):
        return _canonicalize_agent(surface_id[len("agent:/") :])
    raise CanonicalizeError(
        "invalid_scheme",
        f"surface_id does not match canonical scheme list ({CANONICAL_SCHEMES}): {surface_id!r}",
    )


def _canonicalize_file(path: str) -> str:
    """file:// canonicalization (RFC §7.12.1 + DRIFT-2 fix).

    1. Double-realpath to resolve macOS /var → /private/var idempotently.
    2. Strip trailing / unless path is exactly /.
    3. Lowercase if filesystem is case-insensitive.
    4. Re-prefix with file://.
    """
    # strict=True is required to actually catch symlink loops on macOS — without it,
    # os.path.realpath silently returns the loop path. ENOENT is treated as "nonexistent
    # path, fall through" per RFC §7.12.2 — the lease plane does not validate file existence.
    try:
        resolved = os.path.realpath(path, strict=True)
    except OSError as e:
        # ELOOP: 40 on Linux, 62 on macOS; "Too many" guard catches both portably.
        if e.errno in (40, 62) or "Too many" in str(e):
            raise CanonicalizeError("symlink_loop", str(e)) from e
        if e.errno == 2:  # ENOENT — caller passed a nonexistent path; fall through.
            resolved = os.path.realpath(path)
        elif e.errno == 36:  # ENAMETOOLONG
            raise CanonicalizeError("path_too_long", str(e)) from e
        else:
            raise CanonicalizeError("invalid_scheme", str(e)) from e
    # Double-realpath catches macOS /var → /private/var idempotency edge case.
    resolved = os.path.realpath(resolved)

    # Strip trailing / except for root.
    if len(resolved) > 1 and resolved.endswith("/"):
        resolved = resolved.rstrip("/")

    # Lowercase if case-insensitive FS.
    if _is_case_insensitive():
        resolved = resolved.lower()

    return f"file://{resolved}"


def _canonicalize_dialectic(path: str) -> str:
    """dialectic:/ — opaque session id; lowercase only."""
    return f"dialectic:/{path.lower()}"


def _canonicalize_resident(path: str) -> str:
    """resident:/ — opaque resident name; case-sensitive; strip trailing /."""
    if any(ch in path for ch in (" ", "\t", "\n", "?", "#", "&")):
        raise CanonicalizeError(
            "invalid_scheme",
            f"resident:/ surface_id contains reserved character (?, #, &, whitespace): {path!r}",
        )
    return f"resident:/{path.rstrip('/')}"


def _canonicalize_maintenance(path: str) -> str:
    """maintenance:/ — opaque maintenance job surface; case-sensitive.

    Uses the same reserved-character and trailing-slash rules as resident:/,
    but names cleanup/repair coordination surfaces that are not resident
    lifecycle or presence handles. (Migration 049.)
    """
    if any(ch in path for ch in (" ", "\t", "\n", "?", "#", "&")):
        raise CanonicalizeError(
            "invalid_scheme",
            f"maintenance:/ surface_id contains reserved character (?, #, &, whitespace): {path!r}",
        )
    return f"maintenance:/{path.rstrip('/')}"


def _canonicalize_agent(path: str) -> str:
    """agent:/ — opaque ephemeral-agent id; case-sensitive; strip trailing /.

    A PRESENCE surface (unique per agent), routed to remote_heartbeat by the
    plane — not a mutex. Same reserved-char rules as resident:/. (Migration 042.)

    Error-atom parity note (matches resident:/): a `?` in the path is rejected
    here as ``invalid_scheme``, but the Elixir side catches `?` at the top level
    and returns ``reserved_query_string``. The OK path is byte-identical across
    languages; only the error label differs on `?`-bearing input.
    """
    if any(ch in path for ch in (" ", "\t", "\n", "?", "#", "&")):
        raise CanonicalizeError(
            "invalid_scheme",
            f"agent:/ surface_id contains reserved character (?, #, &, whitespace): {path!r}",
        )
    return f"agent:/{path.rstrip('/')}"


def _canonicalize_capture(path: str) -> str:
    """capture:/ — comma-separated member list; sort lexically.

    Closes dialectic missing-from-§7.12 finding: capture:/A,B,C and capture:/B,A,C
    must canonicalize to the same surface_id (same calibration window).
    """
    members = [m.strip() for m in path.split(",") if m.strip()]
    members.sort()
    return f"capture:/{','.join(members)}"
