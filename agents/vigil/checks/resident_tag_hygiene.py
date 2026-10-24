"""Resident tag-hygiene check.

Hits ``/v1/residents/tag_audit`` and emits a critical finding for every active
resident that's missing a required tag. Catches onboarding-path drift before
it manifests as a production incident.

Background: on 2026-04-20 Steward went three days with zero rows in
``core.agent_state`` because its onboarding path stamped ``persistent`` but
not ``autonomous``, and loop-detection pattern 4 silently starved every sync.
The fix was a one-line tag addition — this check makes sure the same
single-tag-stamp class of bug is visible within one Vigil cycle rather than
discovered by coincidence days later.
"""

from __future__ import annotations

import os
import time
from typing import Tuple

import httpx

from .base import CheckResult

RESIDENT_TAG_AUDIT_URL = os.environ.get(
    "RESIDENT_TAG_AUDIT_URL", "http://localhost:8767/v1/residents/tag_audit"
)


def fetch_tag_audit(url: str, timeout: float = 5.0) -> Tuple[bool, dict, str]:
    """Probe the audit endpoint. Returns (ok, payload, error_detail).

    ok=False means the endpoint itself is unreachable or returned non-200.
    ok=True with missing={} means the fleet is healthy.
    """
    start = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout)
        latency_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                return True, data, f"{latency_ms}ms"
            except Exception as e:
                return False, {}, f"JSON parse failed ({latency_ms}ms): {e}"
        return False, {}, f"HTTP {resp.status_code} ({latency_ms}ms)"
    except httpx.ConnectError:
        return False, {}, "unreachable"
    except httpx.TimeoutException:
        return False, {}, f"timeout (>{int(timeout*1000)}ms)"
    except Exception as e:
        return False, {}, str(e)


class ResidentTagHygiene:
    name = "resident_tag_hygiene"
    service_key = "resident_tags"

    async def run(self) -> CheckResult:
        from . import resident_tag_hygiene as _this
        ok, data, detail = _this.fetch_tag_audit(_this.RESIDENT_TAG_AUDIT_URL)

        if not ok:
            # Endpoint unreachable — don't conflate with a real gap.
            # Degrade to warning (governance_health already fires critical
            # when the service is fully down).
            return CheckResult(
                ok=False,
                summary=f"Resident tag audit: endpoint unreachable ({detail})",
                severity="warning",
                fingerprint_key="resident_tag_audit_unreachable",
            )

        missing = data.get("missing") or {}
        if not missing:
            checked = data.get("checked") or []
            ok_count = data.get("ok_count", len(checked))
            return CheckResult(
                ok=True,
                summary=f"Resident tag audit: {ok_count}/{len(checked)} residents carry required tags ({detail})",
            )

        # Missing tags — critical. Emit one summary with all gaps so the
        # finding is one row in the chime block, not N rows.
        gap_parts = sorted(f"{label}:[{','.join(tags)}]" for label, tags in missing.items())
        required = ",".join(data.get("required_tags") or [])
        return CheckResult(
            ok=False,
            summary=(
                f"Resident tag gap — {len(missing)} resident(s) missing required tags "
                f"[{required}]: {'; '.join(gap_parts)}"
            ),
            detail={"missing": missing, "required_tags": data.get("required_tags")},
            severity="critical",
            fingerprint_key=f"resident_tag_gap:{'+'.join(sorted(missing.keys()))}",
        )
