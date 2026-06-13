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
        if context.get("verification_source") == "external_signal":
            return True
    return False


def _has_substrate_evidence(detail: Mapping[str, Any], verification_source: str | None) -> bool:
    if verification_source == "server_observation":
        return True
    for context in _nested_contexts(detail):
        source = _source_text(context)
        if any(marker in source for marker in _TRUSTED_SUBSTRATE_MARKERS):
            return True
    return False


def _has_tool_observation(detail: Mapping[str, Any]) -> bool:
    if _truthy(detail.get("phase5_emitter")):
        return True
    source = _source_text(detail)
    if any(marker in source for marker in _TRUSTED_TOOL_SOURCES):
        return True
    kind = str(detail.get("kind") or "").lower()
    if kind in {"test", "command", "lint", "build", "file_op", "tool_call"} and "exit_code" in detail:
        return True
    if detail.get("tool") and ("exit_code" in detail or "returncode" in detail):
        return True
    for key in ("tool_results", "command_results", "observed_command", "captured_output"):
        if _nonempty(detail.get(key)):
            return True
    return False


def _verified_fields_from_contexts(
    detail: Mapping[str, Any],
    *,
    external_verified: bool,
    substrate_verified: bool,
    tool_observed: bool,
) -> set[str]:
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
) -> CorroborationAssessment:
    """Grade the independent evidence visible for an outcome event."""
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


def enrich_detail_with_corroboration(
    detail: Mapping[str, Any] | None,
    *,
    outcome_type: str,
    verification_source: str | None,
) -> dict[str, Any]:
    """Return a detail copy with corroboration metadata added."""
    payload = _as_dict(detail)
    assessment = assess_outcome_corroboration(
        outcome_type=outcome_type,
        detail=payload,
        verification_source=verification_source,
    )
    payload.update(assessment.as_metadata())
    return payload
