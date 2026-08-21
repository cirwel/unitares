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
    args = ap.parse_args(argv)
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

    uuid = res.get("agent_uuid") or (res.get("raw_governance") or {}).get("uuid")
    if not uuid:
        print(f"no uuid in response, refusing to write anchor: {res}", file=sys.stderr)
        return 1

    try:
        _call("agent", {"action": "update_metadata", "agent_id": uuid,
                        "tags": REQUIRED_TAGS}, token)
    except Exception as exc:
        # Not fatal to the anchor -- but say so loudly, because an untagged
        # identity gets archived by the orphan sweep and the producers would
        # then attribute to a ghost.
        print(f"WARNING: tag stamp failed ({exc}). Stamp {REQUIRED_TAGS} by hand "
              f"or the orphan sweep will archive this identity.", file=sys.stderr)

    ANCHOR.parent.mkdir(parents=True, exist_ok=True)
    tmp = ANCHOR.with_suffix(".tmp")
    tmp.write_text(json.dumps({"agent_uuid": uuid, "display_name": DISPLAY_NAME}))
    os.chmod(tmp, 0o600)
    tmp.replace(ANCHOR)
    print(f"provisioned {uuid}\nanchor: {ANCHOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
