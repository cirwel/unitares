"""Outcome-owned recording, evidence grading, and canonical acknowledgements.

Transport adapters validate caller identity and public input before entering
this component. Prediction binding and delivery durability remain owned by the
existing storage transaction and calibration delivery implementation.
"""

import hashlib
import json
import os
import time as _time
from copy import deepcopy
from typing import Dict, Any, Optional
from src.logging_utils import get_logger
from src.monitor_prediction import (
    consume_prediction,
    get_agent_predictions,
    lookup_prediction,
)
from src.grounding.outcome_anchors import (
    EXCLUSION_REASON_FIXTURE,
    EXCLUSION_REASON_SCRAPED,
    EXCLUSION_REASON_SHADOW,
    EXCLUSION_REASONS_KEY,
    SCRAPED_PREDICTION_SOURCES,
)
from src.outcome_corroboration import (
    GRADE_WEIGHTS,
    TOOL_OBSERVED,
    ceiling_for_verification_source,
    corroboration_upgrade_hint,
    enrich_detail_with_corroboration,
)
logger = get_logger(__name__)

#: Internal, never caller-settable. Missing trust is always capped. The public
#: MCP adapter strips this key before delegating to the owner.
_TRUSTED_INGESTION_KEY = "_trusted_ingestion"


def _get_monitor_runtime():
    """Resolve the shared monitor runtime only when recording an outcome."""
    from importlib import import_module

    return import_module("src.agent_state")


_MIN_TACTICAL_EVIDENCE_WEIGHT = GRADE_WEIGHTS[TOOL_OBSERVED]


def _prediction_request_digest(
    arguments: Dict[str, Any],
    *,
    outcome_type: str,
    outcome_score: float,
    is_bad: bool,
    detail: Dict[str, Any],
    verification_source: str,
) -> str:
    """Digest caller-controlled outcome semantics for durable idempotency.

    Session identifiers describe the submitting process, not the canonical
    outcome, so a retry after restart must not conflict. Registry-derived
    confidence/provenance and EISV are likewise excluded: the first committed
    claim preserves those values and replays them verbatim.
    """
    canonical = {
        "schema": 1,
        "outcome_type": outcome_type,
        "outcome_score": outcome_score,
        "is_bad": is_bad,
        "detail": detail,
        "confidence": arguments.get("confidence"),
        "decision_action": arguments.get("decision_action"),
        "verification_source": verification_source,
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

# Outcome types that are considered "bad" by default
BAD_OUTCOME_TYPES = {"test_failed", "tool_rejected", "drawing_abandoned", "task_failed"}
# watcher_finding_confirmed = Watcher's analytical judgment held (good outcome).
GOOD_OUTCOME_TYPES = {"test_passed", "drawing_completed", "task_completed", "watcher_finding_confirmed"}
# dialectic_resolved is neutral: agreement was reached, but whether the
# resolution holds up under the agreed conditions is a separate later test.
# Recording it closes the dialectic→outcome_events tracking gap so downstream
# back-tests can correlate resumption with subsequent agent state.
# watcher_finding_dismissed is neutral: only a 'fp' dismissal is bad, so the
# caller (agents/watcher/agent.py) passes is_bad explicitly rather than inferring
# it from the type.
NEUTRAL_OUTCOME_TYPES = {"trajectory_validated", "dialectic_resolved", "watcher_finding_dismissed"}  # is_bad determined by score/explicit
VALID_OUTCOME_TYPES = BAD_OUTCOME_TYPES | GOOD_OUTCOME_TYPES | NEUTRAL_OUTCOME_TYPES

_HARD_EXOGENOUS_DETAIL_KEYS = (
    ("tests", "tests"),
    ("commands", "commands"),
    ("files", "files"),
    ("lint", "lint"),
    ("tool_results", "tool_observations"),
)

# Hard-exogenous outcome types eligible for tactical calibration.
# Must be binary pass/fail from real work — not graded scores, not retroactive.
# Both this constant and the gate inside record_outcome must stay in
# sync; the classifier below is driven from _HARD_EXOGENOUS_TYPE_TO_CHANNEL,
# and the recording gate uses HARD_EXOGENOUS_TYPES directly.
from src.calibration_delivery import HARD_EXOGENOUS_CHANNELS
HARD_EXOGENOUS_TYPES = frozenset(HARD_EXOGENOUS_CHANNELS)

_CONTROLLED_FIXTURE_FLAGS = frozenset(
    {
        "synthetic_calibration_fixture",
        "do_not_use_for_live_validation",
        "synthetic_negative_control",
        "do_not_persist",
        "calibration_excluded",
    }
)
_CONTROLLED_FIXTURE_BINDINGS = frozenset({"synthetic_negative_control"})

# Prediction sources where the SERVER supplied the confidence, not the caller.
# `registry` (a prediction registered before the work, consumed by id) and
# `argument` (the caller stated it on this call) are the caller's own number.
# These two are not: the fallback chain scrapes the agent's most recent
# confidence from anywhere — a different turn, a different task, hours earlier —
# and pairs it with THIS outcome. Calibration asks "does stated confidence
# predict outcomes"; a number the agent never stated about this work cannot
# answer that, in either direction.
#
# Measured 2026-07-31 on the `tests` channel (clean epoch since the 2026-07-26
# reset): 198 rows carried FIVE distinct confidence values, 103 of them at
# exactly 0.60 across 12 different agents, and every single row was
# server-scraped (`registry` = 0). The confidence axis was a handful of sticky
# per-agent defaults, so the resulting "calibration" curve measured which agent
# emitted a row, not anyone's judgement. The apparent +0.405 overconfidence in
# the top bin was 37 rows of scraped 1.0 from TWO agents. See #1321 / #1345.
# The canonical set lives beside the fixture rules in src.grounding.outcome_anchors
# so the reader that classifies historical rows cannot drift from the writer.
_SERVER_SCRAPED_PREDICTION_SOURCES = SCRAPED_PREDICTION_SOURCES

_HARD_EXOGENOUS_TYPE_TO_CHANNEL = HARD_EXOGENOUS_CHANNELS


def _coerce_bool_flag(value: Any) -> bool:
    """Interpret a JSON-ish truthy flag (bool/int/str) conservatively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


# Lite eisv_snapshot keys returned by default on outcome acks (#604 dogfood
# 2026-06-24). The actual state numbers and the active source are kept (the
# small primary/behavioral/ode views) plus the compact field glossary; the heavy
# self-description (state_semantics role table, source-meta blocks, the
# sensor-divergence history list) is dropped unless include_semantics=true.
_LITE_SNAPSHOT_KEYS = (
    "primary_eisv",
    "primary_eisv_source",
    "behavioral_eisv",
    "ode_eisv",
    "ode_diagnostics",
    "eisv_labels",
)


def _lite_eisv_snapshot(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a small confirmation-sized view of the EISV snapshot.

    Keeps the field glossary while dropping `state_semantics` (role descriptions
    + hierarchy), the `*_source_meta` blocks, and `sensor_divergence_recent` (a
    list of up to 20 records). Returns ``None`` unchanged so a missing snapshot
    still reads as missing. Gated behind include_semantics / response_mode="full".
    """
    if not snapshot:
        return snapshot
    lite = {k: snapshot[k] for k in _LITE_SNAPSHOT_KEYS if k in snapshot}
    lite["semantics_omitted"] = True
    lite["hint"] = "Pass include_semantics=true for the full EISV ontology."
    return lite


def _classify_hard_exogenous_signal(outcome_type: str, detail: Dict[str, Any]) -> str | None:
    """Return the hard exogenous signal source when this outcome is e-process eligible."""
    channel = _HARD_EXOGENOUS_TYPE_TO_CHANNEL.get(outcome_type)
    if channel:
        return channel
    for key, label in _HARD_EXOGENOUS_DETAIL_KEYS:
        if detail.get(key):
            return label
    return None


def _truthy_detail_flag(value: Any) -> bool:
    """Interpret JSON-ish fixture flags conservatively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _is_controlled_validation_fixture(detail: Dict[str, Any]) -> bool:
    """Return whether an outcome is self-declared controlled fixture evidence."""
    if any(_truthy_detail_flag(detail.get(flag)) for flag in _CONTROLLED_FIXTURE_FLAGS):
        return True
    binding = detail.get("prediction_binding")
    return bool(binding in _CONTROLLED_FIXTURE_BINDINGS)


async def record_outcome(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Record a validated outcome and return its canonical acknowledgement.

    This is the outcome component's in-process API. Transport adapters retain
    their validation and identity/provenance gates; callers must validate
    outcome_type and agent_id before calling. Internal ingestion trust remains
    opt-in through the existing server-controlled argument.

    Recording deliberately runs without a transport timeout wrapper. Bound
    predictions retain the storage owner's binding/outcome/calibration-outbox
    transaction; unbound outcomes retain their existing delivery behavior.
    """
    from src.db import get_db
    from src.mcp_handlers.context import get_context_client_session_id

    outcome_type = arguments["outcome_type"]
    agent_id = arguments["agent_id"]
    mcp_server = _get_monitor_runtime()

    # An outcome can be an agent's first request after a restart. Its open
    # forecasts, and the behavioral state this outcome snapshots, live in the
    # monitor snapshot, which is read only when the monitor loads. Load it
    # before anything below reads the monitor, and only when a prediction_id
    # is supplied, so the prediction can bind with the agent's own state.
    _loaded_monitors = getattr(mcp_server, "monitors", None)
    if (
        arguments.get("prediction_id")
        and isinstance(_loaded_monitors, dict)
        and agent_id not in _loaded_monitors
        and hasattr(mcp_server, "get_or_create_monitor")
    ):
        try:
            mcp_server.get_or_create_monitor(agent_id)
        except Exception as e:  # noqa: BLE001 - fall through to the existing paths
            logger.debug(f"outcome_event: monitor load for prediction lookup failed: {e}")

    # Infer is_bad if not provided
    is_bad = arguments.get("is_bad")
    if is_bad is None:
        is_bad = outcome_type in BAD_OUTCOME_TYPES

    # Infer outcome_score if not provided
    outcome_score = arguments.get("outcome_score")
    if outcome_score is None:
        outcome_score = 0.0 if is_bad else 1.0

    detail = dict(arguments.get("detail") or {})
    submitted_detail = deepcopy(detail)
    session_id = (
        arguments.get("session_id")
        or arguments.get("client_session_id")
        or get_context_client_session_id()
    )

    # Fetch latest EISV snapshot for this agent (ODE state from DB)
    db = get_db()
    eisv = await db.get_latest_eisv_by_agent_id(agent_id)

    eisv_e = eisv["E"] if eisv else None
    eisv_i = eisv["I"] if eisv else None
    eisv_s = eisv["S"] if eisv else None
    eisv_v = eisv["V"] if eisv else None
    eisv_phi = eisv["phi"] if eisv else None
    eisv_verdict = eisv["verdict"] if eisv else None
    eisv_coherence = eisv["coherence"] if eisv else None
    eisv_regime = eisv["regime"] if eisv else None

    # Embed behavioral EISV (observation-first, per-agent) alongside ODE snapshot
    monitor = None
    try:
        monitors = getattr(mcp_server, "monitors", None)
        if isinstance(monitors, dict):
            monitor = monitors.get(agent_id)
        if monitor:
            bstate = getattr(monitor, '_behavioral_state', None)
            if bstate and bstate.confidence > 0:
                detail['behavioral_eisv'] = {
                    'E': round(bstate.E, 4),
                    'I': round(bstate.I, 4),
                    'S': round(bstate.S, 4),
                    'V': round(bstate.V, 4),
                    'confidence': round(bstate.confidence, 4),
                }
    except Exception:
        pass  # Fail-safe: ODE snapshot still recorded

    snapshot = None
    if eisv:
        primary_e, primary_i, primary_s, primary_v = eisv_e, eisv_i, eisv_s, eisv_v
        if monitor:
            try:
                primary_e, primary_i, primary_s, primary_v = monitor.get_primary_eisv()
            except Exception:
                pass
        snapshot_metrics = {
            "E": primary_e,
            "I": primary_i,
            "S": primary_s,
            "V": primary_v,
            "phi": eisv_phi,
            "verdict": eisv_verdict,
            "coherence": eisv_coherence,
            "regime": eisv_regime,
            "ode": {
                "E": eisv_e,
                "I": eisv_i,
                "S": eisv_s,
                "V": eisv_v,
            },
        }
        # Imported at call time, not module scope, for two reasons. It breaks
        # the runtime_queries <-> mcp_handlers cycle: runtime_queries imports
        # src.mcp_handlers.shared at module level, which executes the whole
        # handler package, which lands back here — so a module-scope import
        # made src.services.runtime_queries un-importable on its own (the
        # reason test_zero_observation_honesty carries an import-order anchor).
        # It also resolves the attribute per call, so patching
        # runtime_queries._build_eisv_semantics actually reaches this call site;
        # bound at import time it did not, and the stub in
        # tests/integration/test_outcome_event_calibration_wiring.py was inert.
        from src.services.runtime_queries import build_eisv_semantics

        snapshot = build_eisv_semantics(snapshot_metrics, monitor)
        detail["primary_eisv"] = snapshot.get("primary_eisv")
        detail["primary_eisv_source"] = snapshot.get("primary_eisv_source")
        detail["behavioral_eisv"] = snapshot.get("behavioral_eisv")
        detail["ode_eisv"] = snapshot.get("ode_eisv")
        detail["ode_diagnostics"] = snapshot.get("ode_diagnostics")
        detail["state_semantics"] = snapshot.get("state_semantics")
        detail["snapshot_source"] = "latest_agent_state"
        detail["snapshot_missing"] = False
    else:
        detail["snapshot_source"] = "missing"
        detail["snapshot_missing"] = True

    # Resolve confidence before persisting detail so exports can reconstruct the lane.
    #
    # Prediction resolution is a non-consuming peek. The database claim is the
    # binding authority; the local registry is marked consumed only after a
    # created/existing durable acknowledgement.
    #
    # Resolution order:
    #   1. Explicit `prediction_id` — two-phase lookup against the agent's open
    #      prediction registry. This is the phase-one seam that lets
    #      outcome_event reference a specific (confidence, timestamp) pair
    #      instead of relying on the _prev_confidence temporal proxy.
    #   2. Explicit `confidence` argument — caller overrode registration.
    #   3. Fallback to monitor._prev_confidence (most recent value seen).
    #   4. DB audit trail fallback.
    _confidence: Optional[float] = None
    prediction_id = arguments.get("prediction_id")
    prediction_source = None
    prediction_record = None
    prediction_binding: str = "no_binding"
    _monitors = getattr(mcp_server, "monitors", None) or {}
    _monitor_for_ttl = _monitors.get(agent_id) if isinstance(_monitors, dict) else None
    ttl_seconds = float(getattr(_monitor_for_ttl, "_prediction_ttl_seconds", 3600.0))

    if prediction_id:
        _m = _monitors.get(agent_id) if isinstance(_monitors, dict) else None
        open_predictions = get_agent_predictions(agent_id, _m)
        if open_predictions is not None:
            record_peek = lookup_prediction(open_predictions, prediction_id)
            if record_peek is None:
                prediction_binding = "missing_prediction"
            else:
                age = _time.monotonic() - float(record_peek.get("created_at", 0.0))
                if age > ttl_seconds:
                    prediction_binding = "ttl_expired_fallback"
                else:
                    prediction_record = record_peek
                    _confidence = float(prediction_record.get("confidence"))
                    prediction_source = "registry"
                    prediction_binding = "registry"
        else:
            # No open_predictions available (old monitor or no monitor)
            try:
                if _m is not None:
                    prediction_record = _m.lookup_prediction(prediction_id)
                    if prediction_record is not None:
                        _confidence = float(prediction_record.get("confidence"))
                        prediction_source = "registry"
                        prediction_binding = "registry"
                    else:
                        prediction_binding = "missing_prediction"
            except Exception:
                prediction_binding = "missing_prediction"

    if _confidence is None:
        _raw_conf = arguments.get("confidence")
        if _raw_conf is not None:
            _confidence = float(_raw_conf)
            prediction_source = prediction_source or "argument"
            if prediction_binding == "no_binding":
                prediction_binding = "argument_fallback"

    if _confidence is None:
        try:
            _mon = mcp_server.monitors.get(agent_id)
            prev_confidence = getattr(_mon, "_prev_confidence", None) if _mon else None
            if isinstance(prev_confidence, (int, float)):
                _confidence = float(prev_confidence)
                prediction_source = prediction_source or "prev_confidence_fallback"
                if prediction_binding == "no_binding":
                    prediction_binding = "prev_confidence_fallback"
        except Exception:
            pass

    # Step 4: DB fallback — query audit trail for most recent confidence
    if _confidence is None:
        try:
            db_conf = await db.get_latest_confidence_before(agent_id=agent_id)
            if db_conf is not None:
                _confidence = db_conf
                prediction_source = prediction_source or "audit_trail_fallback"
                if prediction_binding == "no_binding":
                    prediction_binding = "audit_trail_fallback"
        except Exception:
            pass

    decision_action = arguments.get("decision_action")
    if decision_action is None and prediction_record is not None:
        decision_action = prediction_record.get("decision_action")
    if decision_action is None and outcome_type in {"test_passed", "test_failed"}:
        decision_action = "proceed"

    # Pydantic validation (params_step.py) fills defaults before the MCP
    # entry point runs, so the schema default ("agent_reported_tool_result")
    # is already present in `arguments` for that path. In-process callers
    # (Phase-5 evidence loop, dialectic resolution) pass their own value
    # explicitly. Default here is the v1 schema default for safety.
    verification_source = arguments.get("verification_source") or "agent_reported_tool_result"
    # TRUST IS OPT-IN, CAPPING IS THE DEFAULT.
    #
    # An unvouched caller is capped at TOOL_OBSERVED whatever it claims -- not
    # merely when it claims to be an agent. That is deliberate: deriving the cap
    # from `verification_source` alone would re-expose the grader the moment a
    # future public handler forgot the source downgrade, since the schema lets a
    # caller ASK for external_signal.
    #
    # A vouched in-process or operator-gated caller still does not get a blanket
    # pass: its ceiling comes from the provenance it recorded, so a trusted site
    # that emits agent-attested rows (the Phase-5 evidence loop does) keeps those
    # rows capped while its server-observed rows are held to what
    # `server_observation` itself asserts -- substrate_observed, not the top of
    # the scale. Vouching the transport is not vouching the text it carries.
    trusted_ingestion = bool(arguments.pop(_TRUSTED_INGESTION_KEY, False))
    corroboration_ceiling = (
        ceiling_for_verification_source(verification_source)
        if trusted_ingestion
        else TOOL_OBSERVED
    )
    detail = enrich_detail_with_corroboration(
        detail,
        outcome_type=outcome_type,
        verification_source=verification_source,
        ceiling=corroboration_ceiling,
    )

    evidence_weight = float(detail.get("evidence_weight") or 0.0)
    # A row that declares itself a synthetic fixture (e.g. the calibration
    # harness) is PERSISTED for the author's own per-agent analysis but must
    # NEVER train calibration — otherwise a fixture accidentally pointed at live
    # governance would poison the global tactical/strategic channels. This is the
    # functional half of the harness's self-marking (the detail flag alone is
    # only a forensic breadcrumb without this guard). Treat older red-team
    # fixture boundary flags as equivalent calibration exclusions too.
    # Phase-5 shadow rows (UNITARES_PHASE5_EVIDENCE_WRITE=shadow) persist for
    # burn-in analysis but must not train calibration: nothing else consumed
    # the shadow_write flag, so "shadow" mode was shadow in name only — the
    # rows self-grade TOOL_OBSERVED (0.65) via phase5_emitter, exactly the
    # training threshold, and would have caused the distribution shift the
    # deploy gate (phase-5 contract §8) exists to observe first.
    # Third exclusion class, same principle as the two above: a confidence the
    # SERVER scraped rather than the caller stated is not a prediction, so
    # training on it poisons the channel exactly as a misaimed fixture would.
    # Keyed on `prediction_source` (where the number came from), NOT on
    # `prediction_binding` — the binding label can read "missing_prediction"
    # while the caller did supply a confidence by argument, because the
    # `if prediction_binding == "no_binding"` guards above stop the label being
    # overwritten. Gating on the label would exclude caller-supplied rows.
    # Escape hatch, no redeploy needed, for an operator who wants the prior
    # behaviour while the prediction protocol is designed:
    # UNITARES_CALIBRATION_ALLOW_SCRAPED_CONFIDENCE=1.
    scraped_confidence = (
        prediction_source in _SERVER_SCRAPED_PREDICTION_SOURCES
        and os.environ.get(
            "UNITARES_CALIBRATION_ALLOW_SCRAPED_CONFIDENCE", ""
        ).strip().lower() not in {"1", "true", "yes", "on"}
    )
    # Record WHY the row is excluded, under distinct keys, so readers can tell
    # fixture traffic (which the validation instruments must drop) from a
    # scraped confidence (which calibration must not train on but which is
    # still live evidence). `calibration_excluded` stays the single calibration
    # gate; the reasons are what the corrected fixture rule reads.
    exclusion_reasons: list[str] = []
    if _is_controlled_validation_fixture(detail):
        exclusion_reasons.append(EXCLUSION_REASON_FIXTURE)
    if bool(detail.get("shadow_write")):
        exclusion_reasons.append(EXCLUSION_REASON_SHADOW)
    if scraped_confidence:
        exclusion_reasons.append(EXCLUSION_REASON_SCRAPED)
    calibration_excluded = bool(exclusion_reasons)
    hard_exogenous_signal = _classify_hard_exogenous_signal(outcome_type, detail)
    if evidence_weight < _MIN_TACTICAL_EVIDENCE_WEIGHT or calibration_excluded:
        hard_exogenous_signal = None
    eprocess_eligible = bool(hard_exogenous_signal and _confidence is not None)

    detail["reported_confidence"] = _confidence
    detail["decision_action"] = decision_action
    detail["hard_exogenous_signal"] = hard_exogenous_signal
    detail["hard_exogenous"] = bool(hard_exogenous_signal)
    detail["eprocess_eligible"] = eprocess_eligible
    detail["calibration_excluded"] = calibration_excluded
    detail[EXCLUSION_REASONS_KEY] = exclusion_reasons
    detail["confidence_scraped"] = bool(scraped_confidence)
    detail["prediction_id"] = prediction_id
    detail["prediction_source"] = prediction_source
    detail["prediction_binding"] = prediction_binding
    if (
        prediction_record is not None
        and prediction_record.get("checkpoint_id") is not None
        and detail.get("checkpoint_id") is None
    ):
        detail["checkpoint_id"] = prediction_record["checkpoint_id"]

    persistence_status = "created"
    if prediction_id:
        request_digest = _prediction_request_digest(
            arguments,
            outcome_type=outcome_type,
            outcome_score=outcome_score,
            is_bad=is_bad,
            detail=submitted_detail,
            verification_source=verification_source,
        )
        persistence = await db.record_bound_outcome_event(
            agent_id=agent_id,
            prediction_id=prediction_id,
            request_digest=request_digest,
            outcome_type=outcome_type,
            is_bad=is_bad,
            outcome_score=outcome_score,
            session_id=session_id,
            eisv_e=eisv_e,
            eisv_i=eisv_i,
            eisv_s=eisv_s,
            eisv_v=eisv_v,
            eisv_phi=eisv_phi,
            eisv_verdict=eisv_verdict,
            eisv_coherence=eisv_coherence,
            eisv_regime=eisv_regime,
            detail=detail,
            eisv_snapshot=snapshot,
            verification_source=verification_source,
        )
        persistence_status = persistence.get("status", "error")
        if persistence_status == "conflict":
            return {
                "error": "prediction_id was already used for a different outcome",
                "error_code": "PREDICTION_REUSE_CONFLICT",
                "error_category": "state_error",
                "canonical_outcome_id": persistence.get("outcome_id"),
            }
        if persistence_status not in {"created", "existing"}:
            return {
                "error": "Failed to record outcome event (database error)",
                "error_code": "DB_ERROR",
                "error_category": "system_error",
            }

        # Use database-canonical material for both the creating acknowledgement
        # and replay. PostgreSQL REAL values can differ from their input Python
        # float, so retaining the pre-write value here would let the first
        # response and calibration disagree with the persisted row and retry.
        outcome_id = persistence["outcome_id"]
        outcome_type = persistence["outcome_type"]
        outcome_score = persistence["outcome_score"]
        is_bad = persistence["is_bad"]
        detail = persistence["detail"]
        snapshot = persistence.get("eisv_snapshot")
        _confidence = detail.get("reported_confidence")
        decision_action = detail.get("decision_action")
        prediction_source = detail.get("prediction_source")
        prediction_binding = detail.get("prediction_binding")
        evidence_weight = float(detail.get("evidence_weight") or 0.0)
        calibration_excluded = bool(detail.get("calibration_excluded"))
        hard_exogenous_signal = detail.get("hard_exogenous_signal")
        eprocess_eligible = bool(detail.get("eprocess_eligible"))

        # This is only a local cache marker. Failure here cannot revoke the
        # authoritative database binding, and it occurs strictly after commit.
        if prediction_record is not None and open_predictions is not None:
            consume_prediction(
                open_predictions,
                prediction_id,
                ttl_seconds=ttl_seconds,
            )
    else:
        outcome_id = await db.record_outcome_event(
            # Declared, not inferred from the payload: this recorder already
            # graded `detail` under an explicit ceiling, so the shared write
            # path must preserve that verdict rather than re-derive it.
            corroboration_applied=True,
            agent_id=agent_id,
            outcome_type=outcome_type,
            is_bad=is_bad,
            outcome_score=outcome_score,
            session_id=session_id,
            eisv_e=eisv_e,
            eisv_i=eisv_i,
            eisv_s=eisv_s,
            eisv_v=eisv_v,
            eisv_phi=eisv_phi,
            eisv_verdict=eisv_verdict,
            eisv_coherence=eisv_coherence,
            eisv_regime=eisv_regime,
            detail=detail,
            verification_source=verification_source,
        )
        if not outcome_id:
            return {
                "error": "Failed to record outcome event (database error)",
                "error_code": "DB_ERROR",
                "error_category": "system_error",
            }

    logger.info(
        "Recorded outcome: type=%s is_bad=%s score=%.2f agent=%s verdict=%s",
        outcome_type, is_bad, outcome_score, agent_id, eisv_verdict,
    )

    # Record calibration from outcome event (never for self-declared synthetic
    # fixtures — they persist above but do not train the calibration channels).
    if (
        not prediction_id  # Bound outcomes use the durable outbox, including replay.
        and persistence_status == "created"
        and _confidence is not None
        and evidence_weight >= _MIN_TACTICAL_EVIDENCE_WEIGHT
        and not calibration_excluded
    ):
        try:
            from src.calibration import calibration_checker
            calibration_checker.record_prediction(
                confidence=_confidence,
                predicted_correct=(_confidence >= 0.5),
                actual_correct=float(outcome_score),
                agent_id=agent_id,
            )
            # Hard-exogenous outcomes (test_*, task_*) feed tactical calibration.
            # signal_source routes the row to per-channel breakdown in
            # CalibrationChecker.tactical_bin_stats_by_channel; aggregate
            # remains populated for back-compat.
            if outcome_type in HARD_EXOGENOUS_TYPES:
                calibration_checker.record_tactical_decision(
                    confidence=_confidence,
                    decision='proceed',
                    immediate_outcome=not is_bad,
                    signal_source=_HARD_EXOGENOUS_TYPE_TO_CHANNEL[outcome_type],
                    agent_id=agent_id,
                )
        except Exception as e_cal:
            logger.debug(f"Calibration from outcome_event skipped: {e_cal}")

        if eprocess_eligible:
            # S10.2: resolve class_tag from the in-memory agent_metadata cache so
            # the tracker's by-class rollup gets the agent's current class at
            # write time. classify_agent(None) returns "default" — a benign
            # bucket — when the cache lookup misses (uncached agent or transient
            # eviction). On any unexpected error, fall through to None so the
            # tracker writes into UNKNOWN_CLASS_BUCKET; the calibration write
            # itself must not fail because class resolution failed.
            class_tag: Optional[str] = None
            try:
                from src.agent_metadata_model import agent_metadata
                from src.grounding.class_indicator import classify_agent
                class_tag = classify_agent(agent_metadata.get(agent_id))
            except Exception as e_cls:
                logger.debug(f"S10 class lookup failed (defaulting to unknown bucket): {e_cls}")

            try:
                from src.sequential_calibration import sequential_calibration_tracker

                sequential_calibration_tracker.record_exogenous_tactical_outcome(
                    confidence=_confidence,
                    outcome_correct=not is_bad,
                    agent_id=agent_id,
                    class_tag=class_tag,
                    signal_source=hard_exogenous_signal,
                    decision_action=decision_action,
                    outcome_type=outcome_type,
                    prediction_id=prediction_id,
                )
            except Exception as e_seq:
                logger.debug(f"Sequential calibration tracking skipped: {e_seq}")

    # P2 (#604 dogfood 2026-06-24): record_result/outcome_event returned the full
    # EISV ontology (`eisv_snapshot.state_semantics`: 7 role descriptions +
    # hierarchy, plus source-meta and the sensor-divergence history list) on
    # every ack — a large context tax for what is usually a confirmation. Default
    # to a lite snapshot mirroring the read tools; the full ontology is opt-in via
    # include_semantics=true (alias: response_mode="full"). The persisted `detail`
    # row is unaffected — only the response payload is trimmed.
    include_semantics = _coerce_bool_flag(arguments.get("include_semantics"))
    if not include_semantics:
        response_mode = str(arguments.get("response_mode") or "").strip().lower()
        include_semantics = response_mode == "full"
    response_snapshot = snapshot if include_semantics else _lite_eisv_snapshot(snapshot)

    response = {
        "outcome_id": outcome_id,
        "outcome_type": outcome_type,
        "is_bad": is_bad,
        "outcome_score": outcome_score,
        "eisv_snapshot": response_snapshot,
        "prediction_binding": prediction_binding,
        "corroboration_grade": detail.get("corroboration_grade"),
        "evidence_weight": detail.get("evidence_weight"),
        "claim_risk": detail.get("claim_risk"),
        "corroboration_reasons": detail.get("corroboration_reasons"),
        # Response-only, never persisted: advice for this caller, not a fact
        # about the outcome. None once the row reached what its path can earn.
        "corroboration_hint": corroboration_upgrade_hint(
            detail.get("corroboration_grade"), ceiling=corroboration_ceiling
        ),
        "claimed_fields": detail.get("claimed_fields"),
        "verified_fields": detail.get("verified_fields"),
        "unverified_fields": detail.get("unverified_fields"),
        # #1790: a row stamped calibration_excluded is also dropped from the
        # validation inventory by the fixture classifiers, so a caller that
        # never sees the flag cannot know its evidence went nowhere. Surface
        # it (with the source of the confidence) at every recording surface.
        "calibration_excluded": calibration_excluded,
        "prediction_source": prediction_source,
    }
    if prediction_id:
        response["idempotent_replay"] = persistence_status == "existing"
    return response

