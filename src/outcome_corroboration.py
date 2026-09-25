"""Corroboration grading for outcome_event rows.

The classifier is deliberately conservative: agent prose and reference strings
are claims until a substrate, tool, or external verifier is visible in
structured metadata. It returns additive JSON metadata so existing consumers can
keep reading outcome_events without a schema migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


CLAIM_ONLY = "claim_only"
SELF_REPORT_WITH_REFS = "self_report_with_refs"
TOOL_OBSERVED = "tool_observed"
SUBSTRATE_OBSERVED = "substrate_observed"
EXTERNALLY_VERIFIED = "externally_verified"

GRADE_WEIGHTS = {
    CLAIM_ONLY: 0.10,
    SELF_REPORT_WITH_REFS: 0.35,
    TOOL_OBSERVED: 0.65,
    SUBSTRATE_OBSERVED: 0.85,
    EXTERNALLY_VERIFIED: 1.00,
}

GRADE_RISK = {
    CLAIM_ONLY: "high",
    SELF_REPORT_WITH_REFS: "medium",
    TOOL_OBSERVED: "medium",
    SUBSTRATE_OBSERVED: "low",
    EXTERNALLY_VERIFIED: "low",
}

#: Grades ordered weakest -> strongest. A ``ceiling`` clamps a computed grade to
#: at most the named tier: the two top grades assert a NON-AGENT observation, so
#: a path that already knows its caller is an agent attesting its own result can
#: cap there without the grader having to distrust its own vocabulary.
GRADE_ORDER = (
    CLAIM_ONLY,
    SELF_REPORT_WITH_REFS,
    TOOL_OBSERVED,
    SUBSTRATE_OBSERVED,
    EXTERNALLY_VERIFIED,
)

_GRADE_RANK = {grade: rank for rank, grade in enumerate(GRADE_ORDER)}

#: verification_source values that denote a caller attesting its OWN result.
#: Such a row can never be graded above TOOL_OBSERVED -- at write time or when
#: an audit surface re-grades it later -- because the two top grades assert a
#: non-agent observation.
#: Explicit opt-out from the default cap. A caller that has ESTABLISHED trust by
#: some means other than the payload passes this; nothing else lifts the cap.
#: Deliberately not ``None``: omission and "I checked, it is trusted" must not
#: be spelled the same way, because omission is what a new call site does.
NO_CEILING = "__no_ceiling__"

#: Distinguishes "argument omitted" from "argument explicitly None" so that BOTH
#: resolve to the safe default rather than to no cap.
_CEILING_UNSET = object()


#: What each recorded provenance is ENTITLED to assert, not merely whether it
#: is trusted. A vouched provenance lifts the cap only as far as its own claim
#: reaches: ``server_observation`` means the server watched this, which is
#: SUBSTRATE_OBSERVED -- it is not a warrant for the payload to call itself
#: externally verified. Anything absent from this map caps at TOOL_OBSERVED.
_PROVENANCE_CEILINGS = {
    "external_signal": EXTERNALLY_VERIFIED,
    "server_observation": SUBSTRATE_OBSERVED,
}


def ceiling_for_verification_source(verification_source: str | None) -> str:
    """Ceiling implied by a row's recorded provenance. Default-deny.

    Self-attested, unknown and NULL provenance cap at TOOL_OBSERVED. The two
    server-controlled provenances cap at what they themselves assert, which is
    the strongest grade their own label supports -- not at no ceiling.

    This returned NO_CEILING for both server provenances until an external
    review of PR #2316 (gpt-5.6-terra, 2026-09-19) pointed out what that let
    through: a ``server_observation`` row whose CALLER-supplied detail nests
    ``{"verification_source": "external_signal", "verified": true}`` graded
    1.00, because vouching the provenance also stopped clamping the payload.
    Verified before the change and after: 1.00 -> 0.85. A vouched transport is
    not a warrant for the text it carries, which is this module's whole thesis
    applied one level up.

    NULL/unknown was previously left uncapped as "a separate decision". That let
    pre-column rows re-grade to 0.85 on the audit surface built to EXPOSE
    self-labelled rows. Unknown provenance is not evidence of verification, so it
    caps like any other unvouched row.
    """
    return _PROVENANCE_CEILINGS.get(verification_source or "", TOOL_OBSERVED)

_CLAIM_FIELD_FAMILIES = {
    "pr": {
        "pr",
        "prs",
        "pr_number",
        "pr_url",
        "pull_request",
        "pull_requests",
        "pull_request_url",
        "github_pr",
    },
    "commit": {
        "commit",
        "commits",
        "commit_sha",
        "commit_hash",
        "sha",
        "merge_commit",
        "git_commit",
    },
    "ci": {
        "ci",
        "ci_run",
        "ci_status",
        "checks",
        "check_run",
        "workflow_run",
    },
    "test": {
        "test",
        "tests",
        "test_name",
        "test_names",
        "test_command",
        "pytest",
    },
    "command": {
        "command",
        "commands",
        "cmd",
        "exit_code",
        "returncode",
    },
}

_FIELD_BY_KEY = {
    key: family
    for family, keys in _CLAIM_FIELD_FAMILIES.items()
    for key in keys
}

_TRUSTED_EXTERNAL_SOURCES = {
    "ci",
    "github",
    "github_actions",
    "gh",
    "git",
    "local_git",
    "local_command",
    "pytest",
    "test_runner",
}

_TRUSTED_TOOL_SOURCES = {
    "recent_tool_results",
    "tool_result",
    "tool_results",
    "command_result",
    "post_tool_use",
    "with_checkin",
}

_TRUSTED_SUBSTRATE_MARKERS = {
    "server_observation",
    "substrate_observation",
    "substrate_interpretation",
    "trajectory_self_validation",
    "pi_anima_eisv",
    "sensor_sync",
    "get_lumen_context",
}


@dataclass(frozen=True)
class CorroborationAssessment:
    grade: str
    evidence_weight: float
    claim_risk: str
    claimed_fields: list[str]
    verified_fields: list[str]
    unverified_fields: list[str]
    reasons: list[str]

    def as_metadata(self) -> dict[str, Any]:
        return {
            "corroboration_grade": self.grade,
            "evidence_weight": self.evidence_weight,
            "claim_risk": self.claim_risk,
            "claimed_fields": list(self.claimed_fields),
            "verified_fields": list(self.verified_fields),
            "unverified_fields": list(self.unverified_fields),
            "corroboration_reasons": list(self.reasons),
        }


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "verified", "pass", "passed", "ok"}
    return bool(value)


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _iter_mappings(*values: Any):
    for value in values:
        if isinstance(value, Mapping):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    yield item


def _nested_contexts(detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    contexts: list[Mapping[str, Any]] = [detail]
    for key in ("provenance_context", "evidence", "evidence_items", "tool_results", "commands", "tests"):
        contexts.extend(_iter_mappings(detail.get(key)))
    return contexts


def _field_family(key: str) -> str | None:
    normalized = key.lower().strip()
    return _FIELD_BY_KEY.get(normalized)


def _claim_fields(detail: Mapping[str, Any]) -> set[str]:
    fields: set[str] = set()
    for context in _nested_contexts(detail):
        for key, value in context.items():
            family = _field_family(str(key))
            if family and _nonempty(value):
                fields.add(family)
            if str(key).lower().strip() == "kind" and str(value).lower().strip() == "test":
                fields.add("test")
    return fields


def _declared_verified_fields(detail: Mapping[str, Any]) -> set[str]:
    fields: set[str] = set()
    for key in ("verified_fields", "verified_claim_fields"):
        raw = detail.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            for item in raw:
                family = _field_family(str(item)) or str(item).lower().strip()
                if family:
                    fields.add(family)
    return fields


def _has_verified_marker(context: Mapping[str, Any]) -> bool:
    for key in (
        "verified",
        "evidence_verified",
        "externally_verified",
        "command_verified",
        "git_verified",
        "github_verified",
        "ci_verified",
        "substrate_verified",
    ):
        if key in context and _truthy(context.get(key)):
            return True
    status = str(
        context.get("verification_status")
        or context.get("verification_result")
        or ""
    ).lower()
    return status in {"verified", "externally_verified", "passed", "observed"}


def _source_text(context: Mapping[str, Any]) -> str:
    parts = []
    for key in (
        "source",
        "verification_source",
        "evidence_source",
        "evidence_kind",
        "observed_by",
        "captured_by",
        "epistemic_class",
    ):
        value = context.get(key)
        if value is not None:
            parts.append(str(value).lower())
    return " ".join(parts)


def _has_external_evidence(detail: Mapping[str, Any], verification_source: str | None) -> bool:
    if verification_source == "external_signal":
        return True
    for context in _nested_contexts(detail):
        source = _source_text(context)
        if _has_verified_marker(context) and any(s in source for s in _TRUSTED_EXTERNAL_SOURCES):
            return True
        # A nested verification_source is caller-authored dict content like any
        # other key here. It previously short-circuited with NO verified marker,
        # which left this detector asymmetric with _has_substrate_evidence below
        # -- the very asymmetry this change set out to remove, still live one
        # branch lower. The top-level `verification_source` ARGUMENT keeps its
        # short-circuit above: that one is set by server code, not by a payload.
        if (
            context.get("verification_source") == "external_signal"
            and _has_verified_marker(context)
        ):
            return True
    return False


def _has_substrate_evidence(detail: Mapping[str, Any], verification_source: str | None) -> bool:
    if verification_source == "server_observation":
        return True
    for context in _nested_contexts(detail):
        source = _source_text(context)
        if _has_verified_marker(context) and any(
            marker in source for marker in _TRUSTED_SUBSTRATE_MARKERS
        ):
            return True
    return False


#: The single TOOL_OBSERVED trigger the SERVER sets. ``phase5_emitter`` is
#: written by the phase-5 emitter itself (``src/mcp_handlers/updates/phases.py``)
#: and is stripped from caller-supplied detail on the public path, so a caller
#: cannot spell it. Every other trigger below is caller-authored vocabulary: an
#: agent that DESCRIBES a tool call in the right shape reaches the same grade as
#: one the server watched make it.
#:
#: This asymmetry is recorded, not resolved. 0.65 is also the calibration
#: admission threshold (``_MIN_TACTICAL_EVIDENCE_WEIGHT``), so the distinction
#: decides whether a row trains the tactical channel. Narrowing the set is an
#: operator call that wants the distribution first --
#: ``scripts/diagnostics/outcome_evidence_provenance_split.py`` reports it.
SERVER_SET_TOOL_TRIGGERS = frozenset({"phase5_emitter"})

#: The structural shapes ``tool_observation_triggers`` reads. Named constants
#: because ``corroboration_upgrade_hint`` states them to callers: one source,
#: so the advice cannot name a shape the grader does not read.
_TOOL_OBSERVATION_KINDS = ("test", "command", "lint", "build", "file_op", "tool_call")
_TOOL_OBSERVATION_PAYLOAD_KEYS = ("tool_results", "command_results", "observed_command", "captured_output")

#: The key the hint shows for each reference family, where one reads better
#: than the family's alphabetical first. Derived from _CLAIM_FIELD_FAMILIES,
#: never a separate list: a family without a preference (or whose preferred key
#: was renamed away) falls back to its own first key, and every family appears.
_PREFERRED_REF_EXAMPLE = {"pr": "pr_url", "commit": "commit_sha", "ci": "ci_run", "test": "test_command", "command": "exit_code"}
_REF_EXAMPLE_KEYS = {
    family: (
        _PREFERRED_REF_EXAMPLE[family]
        if _PREFERRED_REF_EXAMPLE.get(family) in keys
        else sorted(keys)[0]
    )
    for family, keys in _CLAIM_FIELD_FAMILIES.items()
}


def tool_observation_triggers(detail: Mapping[str, Any]) -> set[str]:
    """Names of the evidence triggers in ``detail`` that reach TOOL_OBSERVED.

    Returns every trigger that fires, not just the first, so a reader can tell
    a server-observed row from one whose only evidence is the caller's own
    description of a tool call. ``_has_tool_observation`` is the boolean view;
    the grading verdict is unchanged either way.
    """
    triggers: set[str] = set()
    if _truthy(detail.get("phase5_emitter")):
        triggers.add("phase5_emitter")
    source = _source_text(detail)
    if any(marker in source for marker in _TRUSTED_TOOL_SOURCES):
        triggers.add("trusted_tool_source")
    kind = str(detail.get("kind") or "").lower()
    if kind in _TOOL_OBSERVATION_KINDS and "exit_code" in detail:
        triggers.add("kind_with_exit_code")
    if detail.get("tool") and ("exit_code" in detail or "returncode" in detail):
        triggers.add("tool_with_return_code")
    for key in _TOOL_OBSERVATION_PAYLOAD_KEYS:
        if _nonempty(detail.get(key)):
            triggers.add(f"payload:{key}")
    return triggers


def _has_tool_observation(detail: Mapping[str, Any]) -> bool:
    return bool(tool_observation_triggers(detail))


def _verified_fields_from_contexts(
    detail: Mapping[str, Any],
    *,
    external_verified: bool,
    substrate_verified: bool,
    tool_observed: bool,
    trust_context_markers: bool = True,
) -> set[str]:
    """Field-level verdicts for the claims in ``detail``.

    ``trust_context_markers=False`` skips the per-context sweep below, whose
    authority comes entirely from caller-supplied ``verified`` markers and
    source strings. A capped grade passes False: a submitting path that cannot
    self-certify its GRADE cannot self-certify those markers either.
    """
    fields = (
        _declared_verified_fields(detail)
        if external_verified or substrate_verified
        else set()
    )
    claimed = _claim_fields(detail)
    if external_verified:
        fields.update(claimed)
    if tool_observed:
        fields.update(claimed & {"command", "test"})
    if not trust_context_markers:
        return fields
    for context in _nested_contexts(detail):
        if not _has_verified_marker(context):
            continue
        source = _source_text(context)
        if not (
            any(s in source for s in _TRUSTED_EXTERNAL_SOURCES)
            or any(s in source for s in _TRUSTED_TOOL_SOURCES)
            or any(s in source for s in _TRUSTED_SUBSTRATE_MARKERS)
            or context.get("verification_source") in {"external_signal", "server_observation"}
        ):
            continue
        for key, value in context.items():
            family = _field_family(str(key))
            if family and _nonempty(value):
                fields.add(family)
    if substrate_verified and "test" in claimed:
        fields.add("test")
    return fields


def assess_outcome_corroboration(
    outcome_type: str,
    detail: Mapping[str, Any] | None = None,
    verification_source: str | None = None,
    ceiling: str | None = _CEILING_UNSET,
) -> CorroborationAssessment:
    """Grade the independent evidence visible for an outcome event.

    DEFAULT-DENY. ``ceiling`` clamps the result, and OMITTING it caps at
    TOOL_OBSERVED -- the two top grades assert that a non-agent observed this,
    which no caller can establish from the payload alone. Passing ``None``
    resolves the same way; only the explicit ``NO_CEILING`` sentinel lifts it.

    The previous default was "no cap", which made every grader call site outside
    the one recorder that remembered to pass a ceiling inherit the unsafe
    behaviour. A 2026-09-19 review found three such sites, one of them live on
    the persistence path, where an already-capped detail was re-graded upward
    before being stored.
    """
    unrecognised_ceiling: str | None = None
    if ceiling is _CEILING_UNSET or ceiling is None:
        # Derived from provenance, not a blunt constant: a blunt TOOL_OBSERVED
        # default would also cap server_observation/external_signal rows, whose
        # short-circuits predate this change and are set by server code rather
        # than by a payload. Provenance-derived keeps default-deny exactly where
        # the risk is -- agent-attested and unknown -- and leaves real ingestion
        # alone. A new call site that forgets still cannot let an agent-supplied
        # payload reach the top two grades.
        ceiling = ceiling_for_verification_source(verification_source)
    elif ceiling != NO_CEILING and ceiling not in _GRADE_RANK:
        # A ceiling this function does not recognise used to disable the clamp
        # SILENTLY, because the clamp below is guarded by `ceiling in
        # _GRADE_RANK`. Default-deny that fails open on a typo is not
        # default-deny: `ceiling="TOOL_OBSERVED"` -- the constant's NAME rather
        # than its lowercase value -- graded 0.85. Found by an external review
        # of PR #2316 (gpt-5.6-terra, 2026-09-19) and reproduced before the fix.
        # Fall back to the provenance default and SAY SO in the reasons, rather
        # than raising: a grading call is on the write path, and a caller's
        # typo should downgrade the claim, never drop the row.
        unrecognised_ceiling = str(ceiling)
        ceiling = ceiling_for_verification_source(verification_source)
    detail_map = _as_dict(detail)
    source = verification_source or detail_map.get("verification_source")
    source = str(source) if source else None

    claimed = _claim_fields(detail_map)
    external = _has_external_evidence(detail_map, source)
    substrate = _has_substrate_evidence(detail_map, source)
    tool = _has_tool_observation(detail_map)
    verified = _verified_fields_from_contexts(
        detail_map,
        external_verified=external,
        substrate_verified=substrate,
        tool_observed=tool,
    )
    unverified = claimed - verified

    reasons: list[str] = []
    if external:
        grade = EXTERNALLY_VERIFIED
        reasons.append("external verification/source present")
    elif substrate:
        grade = SUBSTRATE_OBSERVED
        reasons.append("server or substrate observation present")
    elif tool:
        grade = TOOL_OBSERVED
        reasons.append("structured tool/command result present")
    elif claimed:
        grade = SELF_REPORT_WITH_REFS
        reasons.append("agent report includes references but no verified evidence")
    else:
        grade = CLAIM_ONLY
        reasons.append("no independent evidence beyond the claim")

    if ceiling in _GRADE_RANK and _GRADE_RANK[grade] > _GRADE_RANK[ceiling]:
        reasons.append(
            f"grade capped at {ceiling}: {grade} asserts an observation the "
            "submitting path cannot self-certify"
        )
        grade = ceiling
        # The field-level verdicts were derived from the PRE-cap evidence
        # flags, so leaving them alone made a capped row report its claims
        # verified while a genuinely tool_observed row reports the same fields
        # unverified -- the cap would have made a self-attested row look MORE
        # corroborated than an honest one. Re-derive them against the grade
        # actually awarded. A flag is never raised here, only dropped: capping
        # to TOOL_OBSERVED does not assert a tool observation that the evidence
        # never showed.
        external = external and _GRADE_RANK[grade] >= _GRADE_RANK[EXTERNALLY_VERIFIED]
        substrate = substrate and _GRADE_RANK[grade] >= _GRADE_RANK[SUBSTRATE_OBSERVED]
        tool = tool and _GRADE_RANK[grade] >= _GRADE_RANK[TOOL_OBSERVED]
        verified = _verified_fields_from_contexts(
            detail_map,
            external_verified=external,
            substrate_verified=substrate,
            tool_observed=tool,
            trust_context_markers=False,
        )
        unverified = claimed - verified

    if unrecognised_ceiling is not None:
        reasons.append(
            f"ignored unrecognised ceiling {unrecognised_ceiling!r}; "
            f"fell back to the provenance default {ceiling}"
        )

    if source is None:
        reasons.append("verification_source unset; treated conservatively")
    elif source == "agent_reported_tool_result" and grade in {CLAIM_ONLY, SELF_REPORT_WITH_REFS}:
        reasons.append("verification_source is agent_reported_tool_result")

    if outcome_type == "task_completed" and grade == CLAIM_ONLY:
        reasons.append("task_completed completion claim has no corroborating detail")

    return CorroborationAssessment(
        grade=grade,
        evidence_weight=GRADE_WEIGHTS[grade],
        claim_risk=GRADE_RISK[grade],
        claimed_fields=sorted(claimed),
        verified_fields=sorted(verified),
        unverified_fields=sorted(unverified),
        reasons=reasons,
    )


def corroboration_upgrade_hint(grade: str | None, *, ceiling: str | None) -> str | None:
    """Tell a caller what ``detail`` would have to carry to grade higher.

    The reasons say what was missing; without this, an agent told its claim
    had "no corroborating detail" guessed at key names the grader does not
    read (test_exit_code, artifact_hash, external_verifier_id -- an external
    agent's actual guesses, 2026-09-24). Stating the recognised keys is safe
    because it is not an escalation path: every self-attested row is capped
    at ``ceiling`` whatever it carries, and the hint says so, so no one reads
    the key list as a route to a grade the submitting path cannot reach.

    Only self-attested rows (ceiling TOOL_OBSERVED, the public path's cap) get
    a hint: a vouched in-process emitter is not an agent reading advice, and
    the cap sentence would be false for it. Returns None otherwise, and once
    the grade has reached the cap.
    """
    if grade not in _GRADE_RANK or ceiling != TOOL_OBSERVED:
        return None
    if _GRADE_RANK[grade] >= _GRADE_RANK[TOOL_OBSERVED]:
        return None
    parts: list[str] = []
    if grade == CLAIM_ONLY:
        ref_keys = ", ".join(_REF_EXAMPLE_KEYS.values())
        parts.append(
            f"References in detail ({ref_keys}) raise this to "
            f"{SELF_REPORT_WITH_REFS}."
        )
    parts.append(
        f"A structured result reaches {TOOL_OBSERVED}: "
        f"kind (one of {', '.join(_TOOL_OBSERVATION_KINDS)}) with exit_code, "
        f"tool with exit_code or returncode, or a non-empty "
        f"{' / '.join(_TOOL_OBSERVATION_PAYLOAD_KEYS)}."
    )
    parts.append(
        f"Self-attested detail is capped at {TOOL_OBSERVED} whatever it carries; "
        f"higher grades need server_observation or external_signal provenance, "
        f"which a caller cannot set."
    )
    return " ".join(parts)


def enrich_detail_with_corroboration(
    detail: Mapping[str, Any] | None,
    *,
    outcome_type: str,
    verification_source: str | None,
    ceiling: str | None = _CEILING_UNSET,
) -> dict[str, Any]:
    """Return a detail copy with corroboration metadata added.

    Default-deny, same as the grader: omitting ``ceiling`` caps at TOOL_OBSERVED.
    """
    payload = _as_dict(detail)
    assessment = assess_outcome_corroboration(
        outcome_type=outcome_type,
        detail=payload,
        verification_source=verification_source,
        ceiling=ceiling,
    )
    payload.update(assessment.as_metadata())
    return payload
