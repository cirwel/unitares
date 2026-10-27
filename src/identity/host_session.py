"""Descriptive record of which vendor session an identity was minted in.

An adapter that knows its host's own session id (a harness hook reading the
vendor's hook payload) may declare it at onboard as ``host_session``:
``{harness, session_id, workspace}``. The server stores it once, on the
identity it mints, so a later reader can tell which vendor session to resume
and in which directory.

It is caller-declared context, never identity proof. It does not resolve,
recover, bind or deduplicate identities, and nothing enforces uniqueness:
several governance identities may legitimately name one vendor session (a
resumed session mints a fresh identity with declared lineage), and one
identity may span many processes (orchestrated headless turns resume one
anchor). Readers pick among bindings by time; they never equate the ids.

The workspace is stored but not served by the roster reads; they report only
whether a binding is complete enough to resume.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

HOST_SESSION_SCHEMA = "unitares.host_session.v1"

_HARNESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WORKSPACE_MAX = 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _workspace(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _WORKSPACE_MAX:
        raise ValueError("host_session.workspace must be a path of at most 1024 characters")
    if _CONTROL_RE.search(value):
        raise ValueError("host_session.workspace must not contain control characters")
    if not (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)):
        raise ValueError("host_session.workspace must be an absolute path")
    return value


def normalize_host_session(
    value: Any, *, now: Optional[datetime] = None
) -> Optional[dict[str, Any]]:
    """Validate one declared ``host_session`` and return its stored form.

    ``None`` means nothing was declared. A malformed declaration raises
    ``ValueError``; callers treat that as "record nothing", never as a reason
    to refuse the onboard it rode in on.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("host_session must be an object")
    unknown = set(value) - {"harness", "session_id", "workspace"}
    if unknown:
        raise ValueError(f"host_session has unknown fields: {', '.join(sorted(unknown))}")

    harness = value.get("harness")
    if not isinstance(harness, str) or not _HARNESS_RE.match(harness):
        raise ValueError("host_session.harness must be a short identifier")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError("host_session.session_id must be a short identifier")

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    record: dict[str, Any] = {
        "schema": HOST_SESSION_SCHEMA,
        "harness": harness,
        "session_id": session_id,
        "recorded_at": stamp,
        "source": "caller_declared",
    }
    workspace = _workspace(value.get("workspace"))
    if workspace is not None:
        record["workspace"] = workspace
    return record


def host_session_summary(
    harness: Any, session_id: Any, has_workspace: Any
) -> Optional[dict[str, Any]]:
    """The served form of a stored binding: no workspace, plus ``resumable``.

    Roster queries select the three parts rather than the stored object, so
    the workspace path never leaves the database through them. ``resumable``
    means the record holds enough to attempt a resume (harness, session id
    and workspace). It does not mean the directory still exists or that the
    vendor still has the session.
    """
    if not isinstance(harness, str) or not isinstance(session_id, str):
        return None
    return {
        "harness": harness,
        "session_id": session_id,
        "resumable": has_workspace is True,
    }
