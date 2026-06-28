"""Path safety checks for S19 substrate-claim enrollment.

A binary path is "user-writable" if a same-UID process could replace the
file at that path — by overwriting the file or by replacing it via
parent-directory rename/unlink. The enrollment CLI uses these helpers to
warn loudly when a resident's binary lives somewhere a same-UID adversary
could substitute.

This is informational, not blocking — see proposal v2 §Adversary models
A2-escalated. M3 attests launchd identity and process instance, not binary
immutability unless deployment hardening relocates the binary to a non-user-
writable path (e.g. /opt/homebrew/bin or /usr/local/bin).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def is_path_user_writable(path: str) -> bool:
    """Return True if a same-UID process could replace the file at ``path``.

    Walks the parent chain: if any directory along the chain (up to but
    excluding root ``/``) grants write permission to the current real user,
    returns True. The file itself is also checked when it exists (an
    overwrite-able file is replaceable without parent-dir write).

    Edge cases:
    - Path doesn't exist: walks from the first existing parent.
    - Symlinks: resolved via ``Path.resolve()`` before checking.
    - Relative paths: resolved relative to the current working directory.
    """
    resolved = Path(path).expanduser().resolve()

    if resolved.exists() and os.access(str(resolved), os.W_OK):
        return True

    # Walk parent chain. Path.parent of '/' is '/', which terminates the loop.
    p = resolved if resolved.exists() else resolved.parent
    while p != p.parent:
        if p.exists() and os.access(str(p), os.W_OK):
            return True
        p = p.parent
    return False


def first_user_writable_ancestor(path: str) -> Optional[str]:
    """Return the deepest user-writable path in ``path``'s ancestor chain.

    If the file itself is user-writable, returns the file path. Otherwise
    returns the deepest ancestor directory that is user-writable, or
    ``None`` if no path component up to root grants write access.

    Used by the enrollment CLI to make warning messages specific:
    "binary at /home/user/projects/.../sentinel is user-writable via
    /home/user/projects" rather than just "is user-writable."
    """
    resolved = Path(path).expanduser().resolve()

    if resolved.exists() and os.access(str(resolved), os.W_OK):
        return str(resolved)

    p = resolved if resolved.exists() else resolved.parent
    while p != p.parent:
        if p.exists() and os.access(str(p), os.W_OK):
            return str(p)
        p = p.parent
    return None
