"""Durable sink for verification-floor shadow evaluations — and their denominator.

The verification floor (``governance_core.verification``) runs in shadow on every
deployment that has not opted out: ``GOVERNANCE_VERIFICATION_FLOOR_SHADOW``
defaults to true while ``GOVERNANCE_VERIFICATION_FLOOR`` defaults to false. Shadow
mode exists to accumulate the false-positive/recall record the proposal's own
acceptance gate requires before the floor is enabled
(``docs/proposals/verification-weighted-verdict-v0.md``).

Until this module existed, a shadow firing was written onto the result dict
returned by ``process_update`` and onto ``_last_governance_result``, and nowhere
else. No table, no column, no aggregator. The evidence the enable decision waits
on was being computed once per check-in and discarded (issue #2169). Asked how
often the floor would have fired, the honest answer was neither a number nor
zero: the producer ran and nothing recorded it.

This module is the record. It changes no verdict, no risk, no decision, no flag.

What the shape has to support
-----------------------------
The enable decision does not ask for a log of firings. It asks for a **rate
against a denominator** — how often a *benign* check-in would have been escalated.
A sink that only writes firings can never answer it: it has a numerator and no
way to tell an empty window from an unrecorded one. So the default record mode
emits a row for **every** evaluation, firing or not, and each row carries what is
needed to partition the denominator honestly:

* ``scoreable`` / ``unscoreable_reason`` — the detector abstains below 8
  characters, and those rows are not benign traffic that came back clean.
* ``text_chars`` / ``text_words`` / ``first_person`` — a cheap prose-shape proxy.
  The detector reads English first-person narration, so it is *structurally*
  incapable of firing for a templated status line or a state digest. Those
  callers are **unscored** by this channel, not cleared by it
  (``governance_core/verification.py``, final docstring bullet). A rate computed
  over them understates the false-positive risk of enabling the floor, so the
  reader has to be able to exclude them and say that it did.
* ``measurement_scope`` — ``simulation`` rows are synthetic traffic and must
  never be pooled into a live rate.
* ``record_mode`` — stamped on every row so a later reader can distinguish "no
  non-firing rows occurred" from "this deployment was configured not to write
  them". A zero that cannot name which of those it is, is not evidence
  (``CLAUDE.md``, *Measurement authority*).

Where it lands
--------------
``audit.events`` via :mod:`src.audit_log`, event type ``verification_floor_shadow``
— the same sink and the same monthly-partition retention as ``grounding_shadow``
and ``coherence_gate_shadow``. No migration: the table is generic
(``ts, agent_id, event_type, confidence, payload JSONB``) and already partitioned
and indexed on ``(event_type, ts DESC)``. Choosing an existing sink is deliberate:
a dedicated table would carry its own retention posture, and a rate wants the same
retention as the other shadow instruments it will be read beside.

Volume posture
--------------
Default-on shadow plus a row per evaluation is one small audit row per check-in on
every deployment. That is the cost of a denominator, and it is stated rather than
hidden. ``GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD`` narrows it:

* ``all`` (default) — every evaluation. The only mode from which a rate is
  computable.
* ``firings`` — only rows where the floor would have fired. Numerator only; the
  reader refuses to report a rate from these and says why.
* ``off`` — no rows. Restores the pre-#2169 state of affairs deliberately, which
  is not the same as it happening by accident.

Matched spans
-------------
``matches`` carries the regex-matched excerpts, capped and truncated. This is a
deliberate, bounded capture of caller-supplied text into the audit log, and it is
here because a false-positive pass is not adjudicable without it: a category name
says the detector fired, not whether it was right to. The same spans are already
returned to the caller in the result dict.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

SCHEMA = "verification_floor_shadow_v1"
EVENT_TYPE = "verification_floor_shadow"

#: Which backend produced the signal. The inline path is the deterministic regex
#: floor by design — the local-model backend is 40-70s/call and stays out-of-band.
DETECTOR = "regex_v0"

RECORD_MODE_ENV = "GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD"
RECORD_ALL = "all"
RECORD_FIRINGS = "firings"
RECORD_OFF = "off"
_RECORD_MODES = (RECORD_ALL, RECORD_FIRINGS, RECORD_OFF)

#: Bounds on the matched-span capture. Eight categories exist, so eight spans is
#: the ceiling a single firing can legitimately produce.
MAX_MATCHES = 8
MAX_MATCH_CHARS = 120

#: Mirrors ``score_harm_confession``'s abstention guard. Duplicated as a constant
#: here only to *name* the unscoreable row; the detector remains authoritative.
MIN_SCOREABLE_CHARS = 8

_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|i'll|my|me|we|we're|we've|our)\b")


def record_mode() -> str:
    """Which rows this deployment writes: ``all`` (default), ``firings``, ``off``.

    An unrecognised value falls back to ``all`` with a warning rather than
    silently writing nothing — a typo in a sink flag must not reproduce the
    blind-instrument state this module exists to end.
    """
    raw = (os.getenv(RECORD_MODE_ENV, "") or "").strip().lower()
    if not raw:
        return RECORD_ALL
    if raw in _RECORD_MODES:
        return raw
    logger.warning(
        f"{RECORD_MODE_ENV}={raw!r} is not one of {_RECORD_MODES}; "
        f"recording every evaluation ({RECORD_ALL})"
    )
    return RECORD_ALL


def should_record(would_fire: bool, mode: Optional[str] = None) -> bool:
    """Whether a row with this firing state is written under ``mode``."""
    resolved = mode or record_mode()
    if resolved == RECORD_OFF:
        return False
    if resolved == RECORD_FIRINGS:
        return bool(would_fire)
    return True


def _text_shape(response_text: Optional[str]) -> Dict[str, Any]:
    """Describe the input's shape without retaining it.

    ``first_person`` is a proxy, not a classifier. It says the text contains a
    first-person pronoun, which is a necessary condition for the detector's
    verb-object patterns to fire and nothing more. Read it to *exclude* rows the
    channel could never score, never to certify that a row was scored well.
    """
    text = response_text or ""
    stripped = text.strip()
    return {
        "text_chars": len(stripped),
        "text_words": len(stripped.split()),
        "first_person": bool(_FIRST_PERSON.search(stripped.lower())),
    }


def _bounded_matches(matches: Optional[List[str]]) -> List[str]:
    """Cap and truncate matched spans before they reach durable storage."""
    out: List[str] = []
    for span in list(matches or [])[:MAX_MATCHES]:
        text = str(span)
        if len(text) > MAX_MATCH_CHARS:
            text = text[: MAX_MATCH_CHARS - 3].rstrip() + "..."
        out.append(text)
    return out


def evaluate(
    signal: Any,
    *,
    response_text: Optional[str],
    verdict_before: Optional[str],
    risk_before: float,
    verdict_after: Optional[str],
    risk_after: float,
    measurement_scope: str = "live",
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the row for one shadow evaluation. Pure; applies nothing.

    ``verdict_after`` / ``risk_after`` are the counterfactual — what
    ``apply_verification_floor`` WOULD have produced from the live pre-floor pair.
    They are computed by the caller through the same pure function the enabled
    floor uses, so the row records the real combination rather than a
    reimplementation of it.

    Every evaluation produces a row of the same shape, so an abstention is an
    explicit observation rather than an absence.
    """
    score = float(getattr(signal, "score", 0.0) or 0.0)
    abstained = bool(getattr(signal, "abstained", False))
    would_fire = score > 0.0
    shape = _text_shape(response_text)

    unscoreable_reason: Optional[str] = None
    if not shape["text_chars"]:
        unscoreable_reason = "empty_response_text"
    elif abstained:
        unscoreable_reason = "detector_abstained_below_min_length"

    return {
        "schema": SCHEMA,
        "detector": DETECTOR,
        "record_mode": mode or record_mode(),
        "measurement_scope": measurement_scope,
        # Never an enforcement. Shadow rows carry this for the same reason the
        # result-dict key does: so no downstream reader can mistake a would-fire
        # record for something that happened to an agent.
        "applied": False,
        "evaluated": True,
        "would_fire": would_fire,
        "score": round(score, 4),
        "verdict": getattr(signal, "verdict", None),
        "categories": dict(getattr(signal, "categories", {}) or {}),
        "category_count": len(getattr(signal, "categories", {}) or {}),
        "matches": _bounded_matches(getattr(signal, "matches", None)),
        "abstained": abstained,
        "scoreable": unscoreable_reason is None,
        "unscoreable_reason": unscoreable_reason,
        # The counterfactual, recorded as a delta so the false-positive question
        # ("how many clean check-ins would this have escalated?") is answerable
        # without re-deriving it from the score.
        "verdict_before": verdict_before,
        "verdict_after": verdict_after,
        "risk_before": round(float(risk_before), 6),
        "risk_after": round(float(risk_after), 6),
        "would_escalate_verdict": verdict_after != verdict_before,
        "risk_delta": round(float(risk_after) - float(risk_before), 6),
        **shape,
    }


def attach_live_decision(
    payload: Dict[str, Any],
    *,
    action: Optional[str],
    sub_action: Optional[str],
    live_verdict: Optional[str],
) -> Dict[str, Any]:
    """Stamp the decision the deployment actually took onto the row.

    The verdict delta alone does not say whether enabling the floor would have
    cost anyone a pause. Pairing it with the live action does: the candidate
    false positives are the rows where ``would_escalate_verdict`` is true and the
    deployment proceeded.

    This is the *pre-gap-suppression* action, matching what ``log_auto_attest``
    records on the same check-in, so the two are joinable.
    """
    payload["live_action"] = action
    payload["live_sub_action"] = sub_action
    payload["live_verdict"] = live_verdict
    return payload


def record(audit_logger: Any, agent_id: str, payload: Dict[str, Any]) -> None:
    """Emit the row. Never raises into the check-in path.

    Fail-open by design: this is optional measurement sitting on the mandatory
    check-in path, so a broken audit sink must not cost anyone a check-in. Same
    posture as ``coherence_gate_shadow.record`` and ``_record_sensor_divergence``.
    """
    try:
        audit_logger.log_verification_floor_shadow(
            agent_id=agent_id or "unknown", payload=payload
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"verification_floor_shadow record failed: {exc}")
