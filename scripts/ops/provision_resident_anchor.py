#!/usr/bin/env python3
"""Write a resume anchor for a rostered resident that already exists.

The SDK writes ``~/.unitares/anchors/<name>.json`` at first onboard, and every
later process resumes from it with
``identity(agent_uuid=..., continuity_token=..., resume=true)`` (PATH 0). A
resident minted some other way -- a ``start_session`` call from an orchestrator,
a one-off session that was later put on the roster -- never gets that file. Its
identity is then carried only by a process-local session binding, and the first
process boundary after ``UNITARES_IDENTITY_STRICT=strict`` /
``STRICT_IDENTITY_REQUIRED=true`` lands strands it: the bare resume is refused,
and the two documented alternatives (``force_new=true``, ``parent_agent_id``)
mint a successor rather than resume the identity. The revenue-engine run-up
worker hit exactly this on 2026-09-03 after four successful binding-only
sessions.

This is the operator-side repair. It mints a continuity token whose ``aid``
claim is the resident's UUID, signed with the same secret the governance server
uses (``UNITARES_CONTINUITY_TOKEN_SECRET``, else ``UNITARES_HTTP_API_TOKEN``,
else ``UNITARES_API_TOKEN`` -- the same resolution order as
``src/mcp_handlers/identity/session.py``), proves the server ACCEPTED that token
as ownership proof, and only then writes the anchor. It never mints an identity,
never touches tags, and never repoints an anchor at a different UUID without
``--replace-identity``.

WHY THE VERIFY CHECKS MORE THAN THE UUID (2026-09-17 review). A uuid match is
not proof. ``UNITARES_IDENTITY_STRICT`` defaults to ``log``, and in that mode
PATH 0 logs a warning, broadcasts ``identity_hijack_suspected`` and *resumes
anyway* when the token fails its ownership check
(``identity/handlers.py``, the ``_partc_mode == "log"`` branch). A wrong or
absent signing secret therefore produced a green "resumed, uuid matched" from
the first version of this script. Reproduced live against v2.22.1 with an
expired token: ``success: true``, ``resumed: true``, correct uuid, and
alongside it ``proof_origin: "server_inferred"``, ``caller_proven: false``,
``session_resolution_source: "agent_uuid_direct_fastpath"`` and an explicit
``identity_warnings`` entry ``continuity_token_invalid``. So the verify now
requires the response to say the token was the proof, and refuses on any
``continuity_token_invalid`` warning. An unverifiable anchor is worse than
none: it defers the failure to the resident's next session.

Refusals, all before anything is written:

- the anchor exists and names a DIFFERENT uuid (needs ``--replace-identity``);
- no signing secret in the environment;
- the UUID is unknown, archived, or was not granted ``persistent`` +
  ``autonomous`` at mint (a roster miss -- anchoring to an identity the orphan
  sweep will archive points every later session at a ghost);
- the server's label for the UUID is not ``--name`` lowercased, because the
  FILENAME is the lookup key for ``resident_progress.resolve_resident_uuid``;
- the live resume returns a different UUID, refuses, or resolves by anything
  other than the token.

An anchor that already names the SAME uuid is reported and left alone, exit 0,
so the command is re-runnable.

The token in the anchor may be expired by the time the resident next resumes;
that is fine. PATH 0 verifies the signature and the ``aid`` claim and ignores
``exp`` by design (``extract_token_agent_uuid``), and the resume response
carries a fresh token for the first check-in's rebind. Residents should write
that fresh token back to the anchor at session end, as the SDK does.

    python3 scripts/ops/provision_resident_anchor.py \\
        --agent-uuid <UUID> --name revenue-worker-1 --transport http   # dry run
    python3 scripts/ops/provision_resident_anchor.py \\
        --agent-uuid <UUID> --name revenue-worker-1 --transport http --apply

``--transport`` names how the RESIDENT connects, and is required whenever
UNITARES_UDS_SOCKET is set, which the server's environment always does. A
harness session that resumes over MCP (the revenue-engine worker) is ``http``
and gets a verified token; an SDK resident whose own environment sets the
socket is ``uds`` and gets the uuid-only anchor the SDK itself would write.

Run it under the governance server's interpreter, with the server's environment
loaded, so the signing secret matches and the server modules import.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

GOV_URL = os.environ.get("UNITARES_GOV_URL", "http://127.0.0.1:8767")
# NOTE: honored by src/identity/substrate.py but NOT by
# src/resident_progress/registry.py or the SDK, both of which hardcode
# ~/.unitares/anchors. It exists for tests; an operator who sets it gets an
# anchor the resident will never read.
ANCHOR_DIR = Path(os.environ.get(
    "UNITARES_ANCHORS_DIR", str(Path.home() / ".unitares" / "anchors")))

# Both are PRIVILEGED_TAGS granted only by the onboard classifier when the
# minted name is on UNITARES_RESIDENTS. Their absence means the identity is
# not a resident and the orphan sweep will archive it.
REQUIRED_TAGS = ("persistent", "autonomous")

# The filename is a lookup key, not a label. Keep it to what every anchor
# reader globs for, and refuse anything that could escape ANCHOR_DIR.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# A resume that did NOT resolve by the continuity token proves nothing about
# the token. These are the values the server reports when it did.
_PROVEN_ORIGINS = {"caller_asserted"}
_PROVEN_SOURCES = {"continuity_token", "continuity_token_rebind"}


def _call(name: str, arguments: dict, token: str | None) -> dict:
    """POST one tool call to the governance REST surface and return the envelope."""
    body = json.dumps({"name": name, "arguments": arguments}).encode()
    req = urllib.request.Request(
        f"{GOV_URL}/v1/tools/call", data=body,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _unwrap(envelope: dict) -> dict:
    """``/v1/tools/call`` nests the tool payload under ``result``; tolerate both."""
    inner = envelope.get("result")
    return inner if isinstance(inner, dict) else envelope


def _server_modules():
    """Import the two server helpers, or explain why the script cannot run.

    Kept behind a function so --help, every refusal and the dry-run plan never
    need the server's dependency tree. The first version imported
    make_client_session_id at the top of main(), before the dry-run return,
    so a dry run on a plain interpreter died with ModuleNotFoundError: mcp
    AFTER it had already hit the network.
    """
    try:
        from src.mcp_handlers.identity.session import create_continuity_token
        from src.mcp_handlers.identity.shared import make_client_session_id
    except ImportError as exc:
        raise SystemExit(
            f"cannot import the governance server modules ({exc}).\n"
            "  Run this under the interpreter the governance server runs with,\n"
            "  e.g. its virtualenv, so the token is signed by the same code."
        ) from exc
    return create_continuity_token, make_client_session_id


def _read_agent(agent_uuid: str, api_token: str | None) -> tuple[str | None, set[str], str | None]:
    """Return (status, tags, label) for the UUID via an unbound ``agent get``."""
    got = _unwrap(_call("agent", {"action": "get", "agent_id": agent_uuid}, api_token))
    row = got.get("agent") if isinstance(got.get("agent"), dict) else got
    if got.get("error") or got.get("success") is False:
        raise ValueError(str(got.get("error") or got)[:300])
    status = row.get("status")
    tags = {str(t) for t in (row.get("tags") or [])}
    label = row.get("label") or (row.get("identity_view") or {}).get("current", {}).get("label")
    return (str(status) if status is not None else None), tags, (str(label) if label else None)


def _assurance(body: dict) -> dict:
    """Pull identity_assurance from wherever this response shape carries it."""
    for holder in (body, body.get("identity_context") or {}, body.get("raw_governance") or {}):
        block = holder.get("identity_assurance")
        if isinstance(block, dict):
            return block
    return {}


def _verify_resume(
    agent_uuid: str, token: str, api_token: str | None
) -> tuple[bool, str, str | None]:
    """Resume the UUID with the token and prove the TOKEN was what resolved it.

    Returns (ok, detail, fresh_token). A matching uuid is necessary and not
    sufficient: see the module docstring for the log-mode fall-through that
    makes a uuid-only check a false green.
    """
    try:
        raw = _call("identity",
                    {"agent_uuid": agent_uuid, "continuity_token": token, "resume": True},
                    api_token)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"identity call failed: {exc}", None
    body = _unwrap(raw)
    if raw.get("isError") or body.get("error") or body.get("success") is False:
        return False, f"resume refused: {json.dumps(body)[:600]}", None

    got = (body.get("uuid") or body.get("agent_uuid")
           or (body.get("raw_governance") or {}).get("uuid"))
    if got != agent_uuid:
        return False, f"resume returned uuid={got!r}, expected {agent_uuid!r}", None

    for warning in (body.get("identity_warnings") or []):
        if isinstance(warning, dict) and warning.get("code") == "continuity_token_invalid":
            return False, (
                "the server REJECTED the token and resumed by another route "
                f"({warning.get('resolved_via')!r}). The uuid matched anyway, which is "
                "why this check exists. Most likely the signing secret here differs "
                "from the server's."
            ), None

    assurance = _assurance(body)
    origin = assurance.get("proof_origin")
    source = body.get("session_resolution_source") or assurance.get("session_source")
    if origin not in _PROVEN_ORIGINS and source not in _PROVEN_SOURCES:
        return False, (
            f"resume succeeded but not by the token (proof_origin={origin!r}, "
            f"session_resolution_source={source!r}). An anchor written now would "
            "hold a token the server does not accept."
        ), None

    fresh = (body.get("continuity_token")
             or (body.get("raw_governance") or {}).get("continuity_token"))
    return True, f"token accepted (proof_origin={origin!r})", (str(fresh) if fresh else None)


def _mkdir_private(directory: Path) -> None:
    """Create the directory and missing ancestors at 0o700, touching nothing that exists.

    Mirrors agents/sdk/src/unitares_sdk/utils.py::_mkdir_private. Path.mkdir's
    mode applies to the leaf only, so ~/.unitares would otherwise get umask.
    An EXISTING directory is left exactly as the operator set it: the first
    version chmod'ed it to 0o700 on every run, which silently reverted a
    deliberate 0o750 and, on a symlinked anchors dir, rewrote the target.
    """
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)


def _write_anchor(path: Path, data: dict) -> None:
    """Write the anchor atomically, never exposing the token.

    Mirrors agents/sdk/src/unitares_sdk/utils.py::atomic_write: mkstemp so the
    file is 0o600 from creation (the first version wrote at umask, then
    chmod'ed, leaving a window where a world-readable file held a valid
    token), fsync before rename, unique temp name so concurrent runs cannot
    collide on a fixed ``.tmp``, and cleanup in finally.
    """
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd = None
    tmp = None
    try:
        _mkdir_private(path.parent)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        os.write(fd, payload.encode())
        os.fsync(fd)
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = None
        os.replace(tmp, str(path))
        tmp = None
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-uuid", required=True,
                    help="UUID of the existing rostered identity to anchor.")
    ap.add_argument("--name", required=True,
                    help="Resident name; the anchor is <anchors>/<name lowercased>.json, "
                         "and that filename must match the server's label for the UUID.")
    ap.add_argument("--apply", action="store_true",
                    help="mint, verify and write. Without it, report and change nothing.")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op form of the default; changes nothing.")
    ap.add_argument("--replace-identity", action="store_true",
                    help="allow replacing an anchor that names a DIFFERENT uuid. "
                         "Prints the displaced uuid. Without it such a replacement is refused.")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip BOTH server calls and write an unproven anchor. Last resort "
                         "for an unreachable server; the roster-tag gate is then unchecked too.")
    ap.add_argument("--transport", choices=("http", "uds"),
                    help="how the RESIDENT reaches the server. uds: a persistent resident "
                         "attests by peer credential, so the anchor is uuid-only. http: the "
                         "anchor carries a verified continuity token. Required when "
                         "UNITARES_UDS_SOCKET is set, because that variable is also in the "
                         "server's own environment and says nothing about the resident.")
    args = ap.parse_args(argv)
    if args.dry_run and args.apply:
        print("--dry-run and --apply are contradictory; refusing.", file=sys.stderr)
        return 2
    # The SDK decides uuid-only from the RESIDENT's environment
    # (UnitaresAgent._save_session). This script runs under the SERVER's
    # environment, which sets UNITARES_UDS_SOCKET to the socket the server
    # listens on, so reading it here chose uuid-only for every persistent
    # resident -- including one that resumes over MCP HTTP and needs the token
    # (the revenue-engine worker, 2026-09-25). The resident's transport cannot
    # be inferred from here, so an ambiguous environment must say it.
    transport = args.transport
    if transport is None:
        if os.environ.get("UNITARES_UDS_SOCKET"):
            print("UNITARES_UDS_SOCKET is set, which the server's own environment always\n"
                  "  does, so it does not say how this resident connects. Pass\n"
                  "  --transport http (the resident presents a continuity token, e.g. a\n"
                  "  harness session over MCP) or --transport uds (an SDK resident whose\n"
                  "  own environment sets UNITARES_UDS_SOCKET). Nothing written.",
                  file=sys.stderr)
            return 2
        transport = "http"

    api_token = os.environ.get("UNITARES_HTTP_API_TOKEN")
    name = args.name.lower()
    if not _SAFE_NAME.match(name):
        print(f"--name must match {_SAFE_NAME.pattern} (got {args.name!r}); it is a "
              f"filename, not a display label.", file=sys.stderr)
        return 2
    anchor = ANCHOR_DIR / f"{name}.json"
    agent_uuid = args.agent_uuid.strip().lower()
    if len(agent_uuid) != 36 or agent_uuid.count("-") != 4:
        print(f"--agent-uuid does not look like a UUID: {args.agent_uuid!r}", file=sys.stderr)
        return 2

    existing = None
    if anchor.exists():
        try:
            existing = json.loads(anchor.read_text()).get("agent_uuid")
        except Exception:
            existing = "<unreadable>"
        if existing == agent_uuid:
            print(f"anchor already provisioned for {agent_uuid}\nanchor: {anchor}")
            return 0
        if not args.replace_identity:
            print(f"anchor {anchor} names a DIFFERENT identity: {existing}\n"
                  f"  Refusing to repoint it at {agent_uuid} without --replace-identity.\n"
                  f"  Replacing an anchor silently is how a resident forks.", file=sys.stderr)
            return 1

    if not (os.environ.get("UNITARES_CONTINUITY_TOKEN_SECRET")
            or os.environ.get("UNITARES_HTTP_API_TOKEN")
            or os.environ.get("UNITARES_API_TOKEN")):
        print("no signing secret in the environment; set UNITARES_CONTINUITY_TOKEN_SECRET\n"
              "  (or UNITARES_HTTP_API_TOKEN / UNITARES_API_TOKEN) to the value the\n"
              "  governance server runs with, or the token will not verify.", file=sys.stderr)
        return 1

    status = tags = label = None
    if not args.no_verify:
        try:
            status, tags, label = _read_agent(agent_uuid, api_token)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            print(f"could not read agent {agent_uuid}: {exc}", file=sys.stderr)
            return 1
        if status != "active":
            print(f"agent {agent_uuid} is not active (status={status!r}); refusing to anchor it.",
                  file=sys.stderr)
            return 1
        missing = [t for t in REQUIRED_TAGS if t not in tags]
        if missing:
            print(f"agent {agent_uuid} lacks {missing}: it was not on UNITARES_RESIDENTS when\n"
                  f"  minted, so it is not a resident and the orphan sweep will archive it.\n"
                  f"  Refusing to write an anchor that would point later sessions at a ghost.",
                  file=sys.stderr)
            return 1
        if label is not None and label.lower() != name:
            print(f"server label for {agent_uuid} is {label!r}, but the anchor filename would\n"
                  f"  be {name!r}.json. resident_progress.resolve_resident_uuid looks the\n"
                  f"  anchor up BY FILENAME, so this anchor would be invisible to it.\n"
                  f"  Pass --name {label.lower()!r} instead.", file=sys.stderr)
            return 1

    # A persistent resident on a UDS deployment attests by peer credential, not
    # by token, and the SDK deliberately writes a uuid-only anchor for it
    # (agents/sdk/src/unitares_sdk/agent.py::_save_session). Writing a token
    # into that file would leak a bearer credential the resident never uses.
    uuid_only = transport == "uds" and (tags is None or "persistent" in tags)

    print(f"anchor  : {anchor} ({'replacing ' + str(existing) if existing else 'absent'})")
    print(f"agent   : {agent_uuid} status={status} tags={sorted(tags) if tags else None} "
          f"label={label!r}")
    print(f"shape   : {'uuid-only (UDS deployment)' if uuid_only else 'uuid + session + token'}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to mint, verify and write.")
        return 0

    if uuid_only:
        _write_anchor(anchor, {"agent_uuid": agent_uuid})
        print(f"provisioned {agent_uuid} (uuid-only)\nanchor: {anchor}")
        return 0

    create_continuity_token, make_client_session_id = _server_modules()
    client_session_id = make_client_session_id(agent_uuid)
    token = create_continuity_token(agent_uuid, client_session_id)
    if not token:
        print("token mint returned nothing (secret missing at mint time?)", file=sys.stderr)
        return 1

    stored_token = token
    if args.no_verify:
        print("verify  : SKIPPED (--no-verify); this anchor is UNPROVEN and the roster-tag\n"
              "          gate did not run. The resident's next resume is the first test.")
    else:
        ok, detail, fresh = _verify_resume(agent_uuid, token, api_token)
        if not ok:
            print(f"verify  : FAILED -- {detail}\n  Nothing written.", file=sys.stderr)
            return 1
        print(f"verify  : {detail}")
        if fresh:
            stored_token = fresh

    _write_anchor(anchor, {
        "agent_uuid": agent_uuid,
        "client_session_id": client_session_id,
        "continuity_token": stored_token,
    })
    print(f"provisioned {agent_uuid}\nanchor: {anchor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
