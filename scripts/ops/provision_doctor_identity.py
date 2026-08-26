#!/usr/bin/env python3
"""Mint the doctor layer's shared governance identity and write its anchor.

ONE identity for all doctor producers, not one each. The doctors are a single
layer (diagnose -> bounded-heal -> verify -> escalate), so a shared trajectory
is the honest unit, and it is one addition to the calibration population rather
than five.

⛔This does NOT collapse the outcome label. Every producer keeps its own
``event_type`` (``deploy_drift_finding``, ``lumen_checkin_finding``, ...) so
per-detector precision stays a field read. Pooling identities is survivable;
pooling the label is the confound that made the pooled dialectic-reviewer
number describe neither instrument. It also matters concretely: a detector that
is broken by construction -- ``immortal_lease`` is a structural false positive
for every ``resident:/dispatch/<thread>`` lease -- must show up as one bad
detector, not as a drag on the whole layer.

⛔PREREQUISITE: ``Doctor`` must be in the governance server's
``UNITARES_RESIDENTS`` roster BEFORE running this. The identity needs
``persistent`` + ``autonomous``, both of which are in
``PRIVILEGED_TAGS`` (``src/mcp_handlers/lifecycle/mutation.py``) and cannot be
self-assigned -- the server rejects that by design, since they confer archival
immunity and loop-detection exemption. The only sanctioned grant is the onboard
classifier (``src/grounding/onboard_classifier.py``), which stamps
``RESIDENT_DEFAULT_TAGS`` server-side when the minted ``name`` matches the
roster exactly. Roster first, restart the server, then run this. Running it
against a roster that lacks ``Doctor`` mints an untagged identity that the
orphan sweep archives, and every doctor producer then attributes to a ghost --
so this refuses to write the anchor in that case rather than leaving one.

Idempotent: if the anchor already names a live identity, this reports and
exits 0 without minting a second one.

    python3 scripts/ops/provision_doctor_identity.py --dry-run
    python3 scripts/ops/provision_doctor_identity.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

GOV_URL = os.environ.get("UNITARES_GOV_URL", "http://127.0.0.1:8767")
ANCHOR = Path(os.environ.get(
    "UNITARES_DOCTOR_ANCHOR",
    str(Path.home() / ".unitares" / "anchors" / "doctor.json")))
DISPLAY_NAME = "Doctor"

# Without `persistent` the orphan sweep archives a low-activity identity and
# resurrects it silently, which is the regression Watcher's resident tags exist
# to prevent. `autonomous` exempts it from loop-detection pattern 4: the doctors
# fire on cron ticks, and pattern-4 rejection would starve their state writes.
REQUIRED_TAGS = ["persistent", "autonomous"]


def _call(name: str, arguments: dict, token: str | None) -> dict:
    body = json.dumps({"name": name, "arguments": arguments}).encode()
    req = urllib.request.Request(
        f"{GOV_URL}/v1/tools/call", data=body,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually mint. Without it, report and change nothing.")
    # The usage block documents --dry-run and argparse did not define it, so
    # the FIRST documented step exited 2. Accepted explicitly (and as the
    # default behaviour) rather than dropped from the docs: an operator who
    # types the documented command must not get a usage error.
    ap.add_argument("--dry-run", action="store_true", default=False,
                    help="explicit no-op form of the default; changes nothing.")
    args = ap.parse_args(argv)
    if args.dry_run and args.apply:
        print("--dry-run and --apply are contradictory; refusing.", file=sys.stderr)
        return 2
    token = os.environ.get("UNITARES_HTTP_API_TOKEN")

    if ANCHOR.exists():
        try:
            existing = json.loads(ANCHOR.read_text()).get("agent_uuid")
        except Exception:
            existing = None
        if existing:
            print(f"doctor identity already provisioned: {existing}")
            print(f"anchor: {ANCHOR}")
            return 0

    print(f"anchor  : {ANCHOR} (absent)")
    print(f"would mint display_name={DISPLAY_NAME!r} tags={REQUIRED_TAGS}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to mint.")
        return 0

    try:
        res = _call("start_session",
                    {"force_new": True, "name": DISPLAY_NAME,
                     "client_hint": "unitares doctor layer (shared identity for "
                                    "doctor_check / deploy_drift / lumen_checkin / "
                                    "bridge_liveness findings)"},
                    token)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"mint failed, nothing written: {exc}", file=sys.stderr)
        return 1

    # `/v1/tools/call` is the REST shape: the tool payload is nested under
    # "result", NOT spread at the top level (the native MCP transport is the
    # one with the content/text envelope). Reading `res` directly found no
    # uuid on a mint that had actually SUCCEEDED, so the script exited 1 and
    # left an untagged orphan identity behind -- the exact ghost the tag stamp
    # below exists to prevent. Unwrap once, tolerate either shape.
    body = res.get("result") if isinstance(res.get("result"), dict) else res
    uuid = body.get("agent_uuid") or (body.get("raw_governance") or {}).get("uuid")
    if not uuid:
        print(f"no uuid in response, refusing to write anchor: {res}", file=sys.stderr)
        return 1

    # The tags are NOT stamped here. `persistent` and `autonomous` are in
    # `PRIVILEGED_TAGS` (mcp_handlers/lifecycle/mutation.py), which the server
    # refuses to let an identity self-assign -- correctly, since they confer
    # archival immunity and loop-detection exemption. The sanctioned grant is
    # the onboard classifier: `stamp_default_class_tags` stamps
    # RESIDENT_DEFAULT_TAGS server-side when the minted `name` matches the
    # deployment's UNITARES_RESIDENTS roster exactly. So the roster is the
    # prerequisite, and a mint that comes back untagged means the roster is
    # missing this name -- report that instead of writing an anchor that
    # points at an identity the orphan sweep will archive.
    # The mint envelope carries no tags in `minimal` response_mode, so read
    # them back rather than inferring. A failed read is treated as "missing":
    # writing an anchor we could not verify is the failure mode that costs
    # most, and re-running after a fix is free.
    session_id = body.get("client_session_id")
    try:
        got = _call("agent", {"action": "get", "agent_id": uuid,
                              **({"client_session_id": session_id} if session_id else {})},
                    token)
        gbody = got.get("result") if isinstance(got.get("result"), dict) else got
        agent_row = gbody.get("agent") or gbody
        tags = {str(t) for t in (agent_row.get("tags") or [])}
    except Exception as exc:
        print(f"could not read back tags for {uuid}: {exc}", file=sys.stderr)
        tags = set()
    missing = [t for t in REQUIRED_TAGS if t not in tags]
    if missing:
        print(
            f"minted {uuid} but the server did not grant {missing}.\n"
            f"  {DISPLAY_NAME!r} is almost certainly absent from UNITARES_RESIDENTS on the\n"
            f"  governance server. Privileged tags cannot be self-assigned; add\n"
            f"  {DISPLAY_NAME!r} to the roster, restart the server, then re-run.\n"
            f"  Refusing to write the anchor: an untagged identity is archived by the\n"
            f"  orphan sweep and every doctor producer would then attribute to a ghost.",
            file=sys.stderr)
        return 1

    ANCHOR.parent.mkdir(parents=True, exist_ok=True)
    tmp = ANCHOR.with_suffix(".tmp")
    tmp.write_text(json.dumps({"agent_uuid": uuid, "display_name": DISPLAY_NAME}))
    os.chmod(tmp, 0o600)
    tmp.replace(ANCHOR)
    print(f"provisioned {uuid}\nanchor: {ANCHOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
