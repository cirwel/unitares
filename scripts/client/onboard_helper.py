#!/usr/bin/env python3
"""Onboard helper for UNITARES client hooks.

Owns the flow:

1. Read existing ``.unitares/session.json`` cache (if any).
2. Call ``onboard(force_new=true)``. When the cache has a UUID, pass it as
   ``parent_agent_id`` with ``spawn_reason="new_session"``.
3. If the server reports ``trajectory_required`` (identity exists but lacks
   a verifiable signal), return status=``trajectory_required`` with the
   server's recovery hint. We do NOT auto-retry with ``force_new=true``;
   that is an explicit operator decision, not an automatic one (see commit
   718ccd3 and the identity "never silently substitute" invariant).
4. ``force_new=true`` is always sent on startup. ``--force-new`` only means
   "ignore cached lineage".
5. Only write the cache when onboard succeeded and produced a usable uuid.

Emits a JSON line on stdout with the resolved fields for the shell hook to
consume. Never raises — always returns a dict on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_TIMEOUT = 10.0
CACHE_DIR = ".unitares"
CACHE_FILE = "session.json"


def _env_truthy(value: str | None) -> bool:
    """Parse a boolean-ish env var. Only explicit affirmatives count as True so
    a stray ``UNITARES_ORCHESTRATED=0`` / empty value fails closed to mint."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _slot_filename(slot: str | None) -> str:
    """Return the cache filename, optionally namespaced by a slot key.

    Without a slot, returns the legacy shared "session.json". With a slot
    (typically the Claude Code session_id from the hook input JSON), returns
    "session-<safe-slot>.json". This lets N parallel ``claude`` processes in
    the SAME workspace each maintain their own identity rather than racing
    on a single cache file. See KG note 2026-04-14: "multiple claude agents
    sharing UUID" — that was per-workspace cache + multiple processes.
    """
    if not slot:
        return CACHE_FILE
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    safe = safe[:64]  # keep file names sane
    return f"session-{safe}.json"


# --- IO primitives (separable for tests) -----------------------------------

def _failure_response(error: str, *, reason: str, hint: str = "") -> dict:
    """Return a REST-like tool failure envelope for client-side failures."""
    return {
        "result": {
            "success": False,
            "error": error,
            "recovery": {
                "reason": reason,
                "hint": hint,
            },
        },
    }


def _post_json(url: str, payload: dict, timeout: float, token: str | None) -> dict:
    """POST JSON to ``url`` and return the parsed response or structured failure."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception:
            detail = raw[:500] if "raw" in locals() and raw else str(exc)
            return _failure_response(
                f"HTTP {exc.code} from {url}: {detail}",
                reason="http_error",
                hint="Check the governance server logs for the rejected request.",
            )
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return _failure_response(
            f"POST {url} failed: {exc}",
            reason="transport_error",
            hint=(
                "Check that the governance server is reachable from this process; "
                "sandboxed clients may need network permission."
            ),
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return _failure_response(
            f"Invalid JSON response from {url}: {exc}",
            reason="invalid_json_response",
            hint="Use curl or server logs to inspect /v1/tools/call.",
        )


def _read_cache_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) and data else {}


def _parse_cache_timestamp(payload: dict, path: Path) -> float:
    raw = payload.get("updated_at")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except Exception:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cache_lineage_uuid(payload: dict) -> str:
    for key in ("uuid", "agent_uuid"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _session_cache_paths(cache_dir: Path) -> list[Path]:
    if not cache_dir.is_dir():
        return []
    paths: list[Path] = []
    for path in cache_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == CACHE_FILE:
            paths.append(path)
        elif path.name.startswith("session-") and path.name.endswith(".json"):
            paths.append(path)
    return paths


def _read_cache(workspace: Path, slot: str | None = None) -> dict:
    """Read the best local lineage candidate for this process.

    The exact slot wins first. If a fresh process has a new slot, fall back to
    the newest valid session cache in the workspace, including legacy
    ``session.json``. This mirrors ``session_cache.py list`` and avoids the
    common Codex/adapter failure mode where prior lineage exists only in a
    different slotted file.
    """
    cache_dir = workspace / CACHE_DIR
    primary = cache_dir / _slot_filename(slot)
    primary_payload = _read_cache_file(primary)
    if _cache_lineage_uuid(primary_payload):
        return primary_payload

    candidates: list[tuple[float, dict]] = []
    for path in _session_cache_paths(cache_dir):
        if path == primary:
            continue
        payload = _read_cache_file(path)
        if _cache_lineage_uuid(payload):
            candidates.append((_parse_cache_timestamp(payload, path), payload))

    if not candidates:
        return primary_payload
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _write_cache(workspace: Path, payload: dict, slot: str | None = None) -> None:
    """Atomic cache write with mode 0600.

    Mirrors the write-path contract of ``unitares-governance-plugin/scripts/
    session_cache.py:_write_json`` (S20.1b): atomic via ``mkstemp`` +
    ``os.replace``, mode 0600 via ``fchmod`` on the temp fd before rename.
    The default ``Path.write_text`` inherits umask 022 → mode 0644, leaving
    cached identity world-readable on a typical macOS setup; any same-UID
    process could read the file. S20.3.

    On any write/chmod/replace failure, the temp file is unlinked rather
    than left as a turd in ``.unitares/``.
    """
    path = workspace / CACHE_DIR / _slot_filename(slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Response unwrap -------------------------------------------------------

def unwrap_tool_response(raw: dict) -> dict:
    """Unwrap the REST ``/v1/tools/call`` envelope.

    Handles two shapes:

    * Native MCP: ``{"result": {"content": [{"text": "<json>"}]}}``
    * REST-direct: ``{"result": {...fields...}}``

    Returns the inner dict, or ``{}`` if unrecognizable.
    """
    if not isinstance(raw, dict):
        return {}
    result = raw.get("result", raw)
    if not isinstance(result, dict):
        return {}
    content = result.get("content")
    if isinstance(content, list) and content:
        item = content[0]
        if isinstance(item, dict) and "text" in item:
            try:
                return json.loads(item["text"])
            except (json.JSONDecodeError, TypeError):
                return {}
    return result


def is_successful_onboard(parsed: dict) -> bool:
    """Onboard is successful iff the response has ``success != False`` and a uuid."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("success") is False:
        return False
    return bool(parsed.get("uuid"))


def trajectory_required(parsed: dict) -> bool:
    """Detect the ``trajectory_required`` recovery reason."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("success") is not False:
        return False
    recovery = parsed.get("recovery") or {}
    return isinstance(recovery, dict) and recovery.get("reason") == "trajectory_required"


# --- Core flow -------------------------------------------------------------

def _build_bootstrap_initial_state() -> dict:
    """Bootstrap check-in payload sent by the hook (per onboard-bootstrap-checkin §3.5).

    Hook-driven onboards always claim task_type='introspection' — the agent
    has no real task at session-start. The hook MUST NOT fabricate
    confidence values from session metadata; we omit confidence and
    complexity so the server fills its 0.5 defaults. response_text is also
    omitted so the server's "[bootstrap] " + client_hint composition
    applies.

    Substrate-earned exemption is enforced server-side via
    is_substrate_earned (Phase 2). The hook can safely send initial_state
    unconditionally: substrate-earned identities receive bootstrap.written:
    false with reason='substrate-earned-exempt' and no row is written. In
    practice no substrate-earned resident runs through this hook (they're
    launchd-managed), so the structural protection is "residents don't
    fire SessionStart."
    """
    return {"task_type": "introspection"}


def run_onboard(
    *,
    server_url: str,
    agent_name: str,
    model_type: str,
    workspace: Path,
    slot: str | None = None,
    force_new: bool = False,
    client_session_id: str | None = None,
    orchestrated: bool = False,
    auth_token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    with_bootstrap: bool = True,
    post_json: Callable[[str, dict, float, str | None], dict] = _post_json,
    read_cache: Callable[..., dict] = _read_cache,
    write_cache: Callable[..., None] = _write_cache,
) -> dict:
    """Run the onboard flow. Returns a dict with status info.

    ``slot`` namespaces the cache file so multiple processes in the same
    workspace can each own their own identity (typically the Claude Code
    session_id from hook input). When omitted, falls back to the legacy
    shared session.json — preserves single-process behavior.

    ``client_session_id`` is the **thread-anchor resume** path
    (``UNITARES_CLIENT_SESSION_ID``, provisioned by the BEAM orchestrator for
    conversation-turn agents like the Discord bridge). When set AND
    ``orchestrated`` is True, the helper resumes the SAME governance UUID under
    that stable anchor instead of the default fresh-mint-per-process posture —
    so consecutive turns of one conversation are one resumed agent, not a new id
    every turn. force_new is omitted in this mode (the server defaults
    resume=True); the first turn mints+binds the anchor, later turns resume it.

    ``orchestrated`` is the **fail-closed guard** (the load-bearing safety
    property). This same helper is the onboarder for *normal* interactive
    sessions too — so honoring a bare ``client_session_id`` would be dangerous:
    if that env var ever leaked into a normal session (a stray global export,
    an inherited shell), the session would silently switch to resume mode, and
    two sessions sharing the leaked value would resume onto ONE UUID — the
    ghost-siphon the v2 ontology removed name-claim to prevent. So resume
    requires an EXPLICIT positive signal that this is an orchestrated headless
    turn-child (set ``orchestrated=True`` only for ``claude -p`` children the
    orchestrator spawned; the spawner provisions ``UNITARES_ORCHESTRATED=1``
    alongside the anchor). Absent that signal, the anchor is ignored and the
    helper mints exactly as today — a leaked anchor on an interactive session
    is a no-op, never a siphon. When unset, behavior is byte-identical to
    before (force_new + cached-lineage declaration). An explicit ``force_new``
    still wins — it is a deliberate clean break.

    ``with_bootstrap`` (default True) attaches an initial_state payload so
    the server writes a bootstrap check-in row at t=0 per
 §3.5. Idempotent on resume
    (server returns the existing bootstrap row's state_id).
    """
    url = f"{server_url.rstrip('/')}/v1/tools/call"
    cache = read_cache(workspace, slot)

    anchor = (client_session_id or "").strip()
    parent_agent_id = ""

    arguments: dict[str, Any] = {
        "name": agent_name,
        "model_type": model_type,
    }

    if anchor and orchestrated and not force_new:
        # Thread-anchored resume — ONLY in an explicitly-declared orchestrated
        # context (fail-closed; see ``orchestrated`` above). Stable
        # per-conversation session key, one resumed UUID across turns. No
        # force_new (server default resume=True), no lineage — the turns are the
        # same agent, not a parent/child chain.
        arguments["client_session_id"] = anchor
    else:
        # Default posture (identity.md v2): fresh process = fresh agent. Mint
        # with force_new and declare cached lineage via parent_agent_id. A bare
        # anchor without the orchestration signal lands here — it mints, it does
        # NOT resume-share.
        arguments["force_new"] = True
        if not force_new:
            parent_agent_id = (cache.get("uuid") or cache.get("agent_uuid") or "").strip()
        if parent_agent_id:
            arguments["parent_agent_id"] = parent_agent_id
            arguments["spawn_reason"] = "new_session"

    if with_bootstrap:
        arguments["initial_state"] = _build_bootstrap_initial_state()

    raw = post_json(url, {"name": "onboard", "arguments": arguments}, timeout, auth_token)
    parsed = unwrap_tool_response(raw)

    if not is_successful_onboard(parsed):
        # Per 718ccd3: never auto-retry with a weaker/different identity
        # posture. Surface the error so the operator can decide.
        recovery = parsed.get("recovery") or {}
        return {
            "status": "trajectory_required" if trajectory_required(parsed) else "onboard_failed",
            "error": parsed.get("error", "onboard returned no uuid"),
            "recovery_reason": recovery.get("reason", ""),
            "recovery_hint": recovery.get("hint", ""),
        }

    # Build fresh cache payload — never preserve stale fields.
    # continuity_token / continuity_token_supported are intentionally NOT
    # persisted: per identity.md v2 ontology and S1-a, lineage across
    # process-instances is declared via parent_agent_id, not resumed via
    # cached token. The fields stay in the in-process return value so a
    # caller can use them transiently within the same process if needed.
    # S20.3 — mirrors the plugin helper's v2 cache schema (session_cache.py).
    new_cache = {
        "schema_version": 2,
        "server_url": server_url,
        "agent_name": agent_name,
        "slot": slot or "",
        "uuid": parsed.get("uuid"),
        "agent_id": parsed.get("agent_id") or parsed.get("resolved_agent_id") or "",
        "client_session_id": parsed.get("client_session_id", ""),
        "session_resolution_source": parsed.get("session_resolution_source", ""),
        "display_name": parsed.get("display_name", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent_agent_id:
        new_cache["parent_agent_id"] = parent_agent_id
        new_cache["spawn_reason"] = "new_session"
    write_cache(workspace, new_cache, slot)

    return {
        "status": "ok",
        "uuid": new_cache["uuid"],
        "agent_id": new_cache["agent_id"],
        "client_session_id": new_cache["client_session_id"],
        "continuity_token": parsed.get("continuity_token", ""),
        "session_resolution_source": new_cache["session_resolution_source"],
        "continuity_token_supported": parsed.get("continuity_token_supported", False),
        "display_name": new_cache["display_name"],
    }


# --- CLI -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL))
    parser.add_argument("--name", required=True, help="Agent display name")
    parser.add_argument("--model-type", default="claude-code")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--force-new", action="store_true",
                        help="Create a fresh identity without declaring cached lineage")
    parser.add_argument(
        "--no-bootstrap",
        dest="with_bootstrap",
        action="store_false",
        default=True,
        help="Skip the bootstrap check-in payload. Default is to send "
             "initial_state so the server writes a t=0 anchor (per "
             "onboard-bootstrap-checkin §3.5). Use this for callers that "
             "explicitly want no bootstrap row.",
    )
    parser.add_argument(
        "--slot",
        default=os.environ.get("UNITARES_SESSION_SLOT", ""),
        help="Per-process slot key (typically Claude Code session_id) so "
             "parallel processes in the same workspace don't collide on "
             "the same cache file.",
    )
    parser.add_argument(
        "--client-session-id",
        default=os.environ.get("UNITARES_CLIENT_SESSION_ID", ""),
        help="Stable per-conversation session anchor (typically provisioned "
             "as UNITARES_CLIENT_SESSION_ID by the BEAM orchestrator for "
             "turn-based agents like the Discord bridge). Resumes the SAME "
             "identity across turns instead of minting a new id each turn — but "
             "ONLY when --orchestrated is also set (fail-closed; see below).",
    )
    parser.add_argument(
        "--orchestrated",
        action="store_true",
        default=_env_truthy(os.environ.get("UNITARES_ORCHESTRATED")),
        help="Declare this is an orchestrated headless turn-child (the "
             "orchestrator provisions UNITARES_ORCHESTRATED=1 alongside the "
             "anchor). REQUIRED for --client-session-id to trigger resume; "
             "without it a (possibly leaked) anchor is ignored and the helper "
             "mints — so an interactive session can never resume-share.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    auth_token = os.environ.get("UNITARES_HTTP_API_TOKEN") or None
    workspace = Path(args.workspace).expanduser().resolve()
    slot = (args.slot or "").strip() or None
    client_session_id = (args.client_session_id or "").strip() or None
    result = run_onboard(
        server_url=args.server_url,
        agent_name=args.name,
        model_type=args.model_type,
        workspace=workspace,
        slot=slot,
        force_new=args.force_new,
        client_session_id=client_session_id,
        orchestrated=args.orchestrated,
        with_bootstrap=args.with_bootstrap,
        auth_token=auth_token,
        timeout=args.timeout,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
