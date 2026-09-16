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
``src/mcp_handlers/identity/session.py``), proves the token resumes that UUID
against the live server, and only then writes the anchor. It never mints an
identity, never touches tags, and never overwrites an anchor without ``--force``.

Refusals, all before anything is written:

- the anchor already exists (use ``--force`` to replace it);
- no signing secret in the environment;
- the UUID is unknown, archived, or was not granted ``persistent`` +
  ``autonomous`` at mint (a roster miss -- anchoring to an identity the orphan
  sweep will archive points every later session at a ghost);
- the live resume returns a different UUID, or refuses.

The token in the anchor may be expired by the time the resident next resumes;
that is fine. PATH 0 verifies the signature and the ``aid`` claim and ignores
``exp`` by design (``extract_token_agent_uuid``), and the resume response
carries a fresh token for the first check-in's rebind. Residents should write
that fresh token back to the anchor at session end, as the SDK does.

    python3 scripts/ops/provision_resident_anchor.py \\
        --agent-uuid <UUID> --name revenue-worker-1            # dry run
    python3 scripts/ops/provision_resident_anchor.py \\
        --agent-uuid <UUID> --name revenue-worker-1 --apply

Run it where the governance server's environment is available (the plist's
env, or ``source``d), so the signing secret matches.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

GOV_URL = os.environ.get("UNITARES_GOV_URL", "http://127.0.0.1:8767")
ANCHOR_DIR = Path(os.environ.get(
    "UNITARES_ANCHORS_DIR", str(Path.home() / ".unitares" / "anchors")))

# Both are PRIVILEGED_TAGS granted only by the onboard classifier when the
# minted name is on UNITARES_RESIDENTS. Their absence means the identity is
# not a resident and the orphan sweep will archive it.
REQUIRED_TAGS = ("persistent", "autonomous")


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


def _read_agent(agent_uuid: str, api_token: str | None) -> tuple[str | None, set[str]]:
    """Return (status, tags) for the UUID via an unbound ``agent get``."""
    got = _unwrap(_call("agent", {"action": "get", "agent_id": agent_uuid, "lite": False},
                        api_token))
    row = got.get("agent") if isinstance(got.get("agent"), dict) else got
    status = row.get("status")
    tags = {str(t) for t in (row.get("tags") or [])}
    return (str(status) if status is not None else None), tags


def _mint_token(agent_uuid: str, client_session_id: str) -> str | None:
    """Sign a continuity token for the UUID with the server's secret, or None."""
    # Imported lazily so --help and the refusal paths never load server code.
    from src.mcp_handlers.identity.session import create_continuity_token

    return create_continuity_token(agent_uuid, client_session_id)


def _verify_resume(agent_uuid: str, token: str, api_token: str | None) -> tuple[bool, str, str | None]:
    """Resume the UUID with the token on the live server.

    Returns (ok, detail, fresh_token). ``ok`` requires the response to carry the
    same UUID and no error; anything else is a refusal, reported verbatim.
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
    got = body.get("agent_uuid") or (body.get("raw_governance") or {}).get("uuid") or body.get("uuid")
    if got != agent_uuid:
        return False, f"resume returned uuid={got!r}, expected {agent_uuid!r}", None
    fresh = body.get("continuity_token") or (body.get("raw_governance") or {}).get("continuity_token")
    return True, "resumed", (str(fresh) if fresh else None)


def _write_anchor(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-uuid", required=True,
                    help="UUID of the existing rostered identity to anchor.")
    ap.add_argument("--name", required=True,
                    help="Resident name; the anchor is <anchors>/<name lowercased>.json.")
    ap.add_argument("--apply", action="store_true",
                    help="mint, verify and write. Without it, report and change nothing.")
    ap.add_argument("--force", action="store_true",
                    help="replace an anchor that already exists for this name.")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the live resume check (only when the server is unreachable; "
                         "the anchor is then unproven).")
    args = ap.parse_args(argv)

    api_token = os.environ.get("UNITARES_HTTP_API_TOKEN")
    anchor = ANCHOR_DIR / f"{args.name.lower()}.json"
    agent_uuid = args.agent_uuid.strip().lower()
    if len(agent_uuid) != 36 or agent_uuid.count("-") != 4:
        print(f"--agent-uuid does not look like a UUID: {args.agent_uuid!r}", file=sys.stderr)
        return 2

    if anchor.exists() and not args.force:
        try:
            existing = json.loads(anchor.read_text()).get("agent_uuid")
        except Exception:
            existing = "<unreadable>"
        print(f"anchor already exists: {anchor} (agent_uuid={existing})\n"
              f"  Refusing to replace it without --force.", file=sys.stderr)
        return 1

    if not (os.environ.get("UNITARES_CONTINUITY_TOKEN_SECRET")
            or os.environ.get("UNITARES_HTTP_API_TOKEN")
            or os.environ.get("UNITARES_API_TOKEN")):
        print("no signing secret in the environment; set UNITARES_CONTINUITY_TOKEN_SECRET\n"
              "  (or UNITARES_HTTP_API_TOKEN / UNITARES_API_TOKEN) to the value the\n"
              "  governance server runs with, or the token will not verify.", file=sys.stderr)
        return 1

    try:
        status, tags = _read_agent(agent_uuid, api_token)
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

    from src.mcp_handlers.identity.shared import make_client_session_id

    client_session_id = make_client_session_id(agent_uuid)
    print(f"anchor  : {anchor} ({'exists, --force' if anchor.exists() else 'absent'})")
    print(f"agent   : {agent_uuid} status={status} tags={sorted(tags)}")
    print(f"session : {client_session_id}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to mint, verify and write.")
        return 0

    token = _mint_token(agent_uuid, client_session_id)
    if not token:
        print("token mint returned nothing (secret missing at mint time?)", file=sys.stderr)
        return 1

    stored_token = token
    if args.no_verify:
        print("verify  : SKIPPED (--no-verify); the anchor is unproven until first resume")
    else:
        ok, detail, fresh = _verify_resume(agent_uuid, token, api_token)
        if not ok:
            print(f"verify  : FAILED -- {detail}\n  Nothing written.", file=sys.stderr)
            return 1
        print(f"verify  : {detail} (uuid matched)")
        if fresh:
            stored_token = fresh

    _write_anchor(anchor, {
        "agent_uuid": agent_uuid,
        "client_session_id": client_session_id,
        "continuity_token": stored_token,
        "display_name": args.name,
    })
    print(f"provisioned {agent_uuid}\nanchor: {anchor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
