"""
Phase B promotion-eligibility transition detector for Sentinel.

Hooks onto the existing `conflict_held_by_other` batched alarm cycle: when a
conflict batch fires for a surface_kind, query Core's read-only
`GET /v1/lease-plane/phase-b/{surface_kind}` endpoint and emit a transition
finding only when one or more §6.1 criteria change status vs. the last recorded
verdict (or when overall ``promotable`` flips).

Spec: §6.1 / §6.2 ordering.

Boundary contract: this client module never spawns `evaluate_phase_b_promotion.py`,
never imports `psycopg2` or `src.*`, and never opens a direct PostgreSQL connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from agents.common.http_client import (
    resolve_governance_base_url,
    send_with_bearer_fallback,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_PATH = REPO_ROOT / "data" / "lease_plane" / "last_phase_b_verdict.json"
PHASE_B_HTTP_TIMEOUT_S = 20.0


@dataclass
class CriterionTransition:
    number: int
    name: str
    previous_status: str | None  # None on first observation
    current_status: str
    detail: str


@dataclass
class PhaseBTransition:
    surface_kind: str
    promotable_now: bool
    promotable_before: bool | None  # None on first observation
    criteria: list[CriterionTransition] = field(default_factory=list)
    summary: str = ""

    @property
    def is_meaningful(self) -> bool:
        """True iff any criterion changed or `promotable` flipped."""
        if self.promotable_before is None:
            return self.promotable_now
        if self.promotable_now != self.promotable_before:
            return True
        return any(c.previous_status != c.current_status for c in self.criteria)


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _run_evaluator(
    surface_kind: str,
    *,
    api_url: str | None = None,
    api_token: str | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Query Core's `GET /v1/lease-plane/phase-b/{surface_kind}` endpoint."""
    base_url = resolve_governance_base_url(api_url)
    url = f"{base_url}/v1/lease-plane/phase-b/{surface_kind}"

    if http_client is not None:
        resp = send_with_bearer_fallback(
            lambda hdrs: http_client.get(url, headers=hdrs, timeout=PHASE_B_HTTP_TIMEOUT_S),
            explicit_token=api_token,
            base_url=base_url,
        )
    else:
        with httpx.Client() as client:
            resp = send_with_bearer_fallback(
                lambda hdrs: client.get(url, headers=hdrs, timeout=PHASE_B_HTTP_TIMEOUT_S),
                explicit_token=api_token,
                base_url=base_url,
            )

    if resp.status_code >= 400:
        raise RuntimeError(
            f"GET /v1/lease-plane/phase-b/{surface_kind} failed: "
            f"HTTP {resp.status_code} {resp.text[:200]}"
        )
    body = resp.json()
    if not isinstance(body, dict) or not body.get("success"):
        raise RuntimeError(
            f"GET /v1/lease-plane/phase-b/{surface_kind} rejected: {body!r}"
        )
    return body


def _diff_against_previous(
    surface_kind: str, current: dict[str, Any], previous: dict[str, Any] | None
) -> PhaseBTransition:
    """Build a PhaseBTransition by comparing current evaluator output to prior."""
    previous_promotable = previous.get("promotable") if previous else None
    previous_statuses = (
        {int(k): v for k, v in previous.get("criteria_status", {}).items()}
        if previous
        else {}
    )

    transitions: list[CriterionTransition] = []
    for c in current["criteria"]:
        prior = previous_statuses.get(c["number"])
        if prior != c["status"]:
            transitions.append(
                CriterionTransition(
                    number=c["number"],
                    name=c["name"],
                    previous_status=prior,
                    current_status=c["status"],
                    detail=c["detail"],
                )
            )

    transition = PhaseBTransition(
        surface_kind=surface_kind,
        promotable_now=current["promotable"],
        promotable_before=previous_promotable,
        criteria=transitions,
    )
    transition.summary = _format_summary(transition)
    return transition


def _format_summary(transition: PhaseBTransition) -> str:
    if transition.promotable_now and transition.promotable_before is not True:
        verdict = (
            f"[lease-plane] {transition.surface_kind}: "
            f"PROMOTABLE — all §6.1 criteria PASS or N/A"
        )
    elif transition.promotable_before is True and not transition.promotable_now:
        verdict = (
            f"[lease-plane] {transition.surface_kind}: "
            f"REGRESSED — was PROMOTABLE, now NOT"
        )
    else:
        verdict = f"[lease-plane] {transition.surface_kind}:"

    bullets = [
        f"§6.1.{c.number} ({c.name}): "
        f"{c.previous_status or 'unobserved'} → {c.current_status}"
        for c in transition.criteria
    ]
    if not bullets:
        return verdict
    return verdict + " " + "; ".join(bullets)


def _to_state_record(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluated_at": report["evaluated_at"],
        "promotable": report["promotable"],
        "criteria_status": {str(c["number"]): c["status"] for c in report["criteria"]},
    }


def detect_transitions(
    surface_kinds: list[str],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    api_url: str | None = None,
    api_token: str | None = None,
) -> list[PhaseBTransition]:
    """Run the Phase-B check for each surface_kind and return meaningful transitions."""
    if not surface_kinds:
        return []
    state = _load_state(state_path)
    transitions: list[PhaseBTransition] = []

    kwargs: dict[str, Any] = {}
    if api_url is not None:
        kwargs["api_url"] = api_url
    if api_token is not None:
        kwargs["api_token"] = api_token

    for surface_kind in dict.fromkeys(surface_kinds):  # preserve order, dedupe
        try:
            report = _run_evaluator(surface_kind, **kwargs)
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as e:
            raise PhaseBEvaluatorError(surface_kind, str(e)) from e

        previous = state.get(surface_kind)
        transition = _diff_against_previous(surface_kind, report, previous)
        if transition.is_meaningful:
            transitions.append(transition)
        state[surface_kind] = _to_state_record(report)

    _save_state(state_path, state)
    return transitions


class PhaseBEvaluatorError(Exception):
    def __init__(self, surface_kind: str, reason: str):
        super().__init__(f"phase-B evaluator failed for surface_kind='{surface_kind}': {reason}")
        self.surface_kind = surface_kind
        self.reason = reason
