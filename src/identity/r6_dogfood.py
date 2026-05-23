"""R6 dogfood helpers for harness-substrate plurality experiments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from src.identity.s22_h5_comparison import (
    S22H5Entry,
    normalize_s22_h5_entry,
    normalize_s22_harness,
)


SUPPORTED_R6_EXPERIMENTS = ("h1", "h3", "h7", "h8")


@dataclass(frozen=True)
class R6DogfoodSpec:
    experiment_id: str
    title: str
    default_task_outcome: str


R6_DOGFOOD_SPECS = {
    "h1": R6DogfoodSpec(
        experiment_id="h1",
        title="R6 H1 same UUID, different Hermes model",
        default_task_outcome="model-contrast-entry",
    ),
    "h3": R6DogfoodSpec(
        experiment_id="h3",
        title="R6 H3 same Hermes memory, fresh UNITARES UUID",
        default_task_outcome="fresh-uuid-memory-entry",
    ),
    "h7": R6DogfoodSpec(
        experiment_id="h7",
        title="R6 H7 Hermes tool-surface perturbation",
        default_task_outcome="tool-surface-contrast-entry",
    ),
    "h8": R6DogfoodSpec(
        experiment_id="h8",
        title="R6 H8 Hermes transport perturbation",
        default_task_outcome="transport-contrast-entry",
    ),
}


def default_r6_comparison_key(
    experiment_id: str,
    *,
    today: Optional[date] = None,
) -> str:
    experiment_id = _normalize_experiment_id(experiment_id)
    observed_day = today or date.today()
    return f"r6-{experiment_id}-{observed_day.isoformat()}"


def build_r6_dogfood_payloads(
    experiment_id: str,
    *,
    comparison_key: Optional[str] = None,
    model: str = "gpt-5.5",
    variant_model: Optional[str] = None,
    memory_context: str = "same-hermes-memory",
    parent_agent_id: Optional[str] = None,
    baseline_tool_surface: Any = None,
    variant_tool_surface: Any = None,
    variant_transport: Optional[str] = None,
    today: Optional[date] = None,
) -> list[dict[str, Any]]:
    """Build ready-to-send R6 dogfood payload templates."""
    experiment_id = _normalize_experiment_id(experiment_id)
    key = _clean_text(comparison_key) or default_r6_comparison_key(
        experiment_id,
        today=today,
    )
    if experiment_id == "h1":
        return _build_h1_payloads(
            key,
            model=model,
            variant_model=variant_model,
            memory_context=memory_context,
        )
    if experiment_id == "h3":
        return _build_h3_payloads(
            key,
            model=model,
            memory_context=memory_context,
            parent_agent_id=parent_agent_id,
        )
    if experiment_id == "h7":
        return _build_h7_payloads(
            key,
            model=model,
            memory_context=memory_context,
            baseline_tool_surface=baseline_tool_surface,
            variant_tool_surface=variant_tool_surface,
        )
    if experiment_id == "h8":
        return _build_h8_payloads(
            key,
            model=model,
            memory_context=memory_context,
            variant_transport=variant_transport,
        )
    raise ValueError(f"unsupported R6 experiment: {experiment_id}")


def assess_r6_dogfood_entries(
    entries: Sequence[S22H5Entry | Mapping[str, Any]],
    *,
    experiment_id: str,
    comparison_key: Optional[str] = None,
) -> dict[str, Any]:
    """Assess whether collected S22 rows satisfy an R6 dogfood experiment."""
    experiment_id = _normalize_experiment_id(experiment_id)
    target_key = _clean_text(comparison_key)
    normalized = [
        entry
        for entry in (_ensure_entry(raw) for raw in entries)
        if entry is not None
    ]
    if target_key:
        normalized = [
            entry for entry in normalized if entry.comparison_key == target_key
        ]
    hermes_entries = [
        entry
        for entry in normalized
        if normalize_s22_harness(entry.harness_type) == "hermes"
        and entry.is_comparable
    ]

    if experiment_id == "h1":
        decision, reason = _assess_h1(hermes_entries)
    elif experiment_id == "h3":
        decision, reason = _assess_h3(hermes_entries)
    elif experiment_id == "h7":
        decision, reason = _assess_h7(hermes_entries)
    elif experiment_id == "h8":
        decision, reason = _assess_h8(hermes_entries)
    else:
        raise ValueError(f"unsupported R6 experiment: {experiment_id}")

    models = sorted({entry.model for entry in hermes_entries if entry.model})
    agent_ids = sorted({entry.agent_id for entry in hermes_entries if entry.agent_id})
    memory_contexts = sorted({
        entry.memory_context for entry in hermes_entries if entry.memory_context
    })
    transports = sorted({entry.transport for entry in hermes_entries if entry.transport})
    tool_surfaces = sorted({
        ",".join(entry.tool_surface)
        for entry in hermes_entries
        if entry.tool_surface
    })
    return {
        "experiment_id": experiment_id,
        "comparison_key": target_key,
        "decision": decision,
        "reason": reason,
        "entry_count": len(normalized),
        "hermes_comparable_entry_count": len(hermes_entries),
        "distinct_models": models,
        "distinct_agent_ids": agent_ids,
        "distinct_memory_contexts": memory_contexts,
        "distinct_transports": transports,
        "distinct_tool_surfaces": tool_surfaces,
        "recommendations": _recommendations(experiment_id, reason),
    }


def _build_h1_payloads(
    comparison_key: str,
    *,
    model: str,
    variant_model: Optional[str],
    memory_context: str,
) -> list[dict[str, Any]]:
    spec = R6_DOGFOOD_SPECS["h1"]
    variant = _clean_text(variant_model) or "REPLACE_WITH_SECOND_HERMES_MODEL"
    return [
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome="baseline-model-entry",
            response_text=(
                "R6 H1 baseline entry: same UNITARES UUID through Hermes "
                f"using model {model}."
            ),
            model=model,
            memory_context=memory_context,
            locus={"r6_condition": "same_unitares_uuid"},
        ),
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome="variant-model-entry",
            response_text=(
                "R6 H1 variant entry: same UNITARES UUID through Hermes "
                f"using model {variant}."
            ),
            model=variant,
            memory_context=memory_context,
            locus={"r6_condition": "same_unitares_uuid"},
        ),
        _kg_note_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome=spec.default_task_outcome,
            summary="R6 H1 Hermes model contrast note",
            details=(
                "Compare the two H1 entries for the same UNITARES identity. "
                "The intended distinction is model/harness substrate variance, "
                "not identity lineage."
            ),
            model=model,
            memory_context=memory_context,
        ),
    ]


def _build_h3_payloads(
    comparison_key: str,
    *,
    model: str,
    memory_context: str,
    parent_agent_id: Optional[str],
) -> list[dict[str, Any]]:
    spec = R6_DOGFOOD_SPECS["h3"]
    onboard_args: dict[str, Any] = {
        "force_new": True,
        "resume": False,
        "client_hint": "hermes",
        "model_type": model,
        "spawn_reason": "new_session",
    }
    parent = _clean_text(parent_agent_id)
    if parent:
        onboard_args["parent_agent_id"] = parent

    return [
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome="pre-fresh-uuid-entry",
            response_text=(
                "R6 H3 baseline UNITARES UUID entry through Hermes before "
                "minting the fresh identity. This establishes the memory "
                "context that the fresh UUID will also report."
            ),
            model=model,
            memory_context=memory_context,
            locus={
                "r6_condition": "current_unitares_uuid",
                "memory_identity_distinction": True,
            },
        ),
        {"name": "onboard", "arguments": onboard_args},
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome=spec.default_task_outcome,
            response_text=(
                "R6 H3 fresh UNITARES UUID entry through Hermes with the "
                "same Hermes memory context. This records memory inheritance "
                "as distinct from identity inheritance."
            ),
            model=model,
            memory_context=memory_context,
            locus={
                "r6_condition": "fresh_unitares_uuid",
                "memory_identity_distinction": True,
            },
        ),
        _kg_note_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome=spec.default_task_outcome,
            summary="R6 H3 memory/identity distinction note",
            details=(
                "Hermes memory continuity is not UNITARES identity continuity. "
                "This H3 entry should be read as a fresh UNITARES subject "
                "with inherited or shared harness memory context."
            ),
            model=model,
            memory_context=memory_context,
        ),
    ]


def _build_h7_payloads(
    comparison_key: str,
    *,
    model: str,
    memory_context: str,
    baseline_tool_surface: Any,
    variant_tool_surface: Any,
) -> list[dict[str, Any]]:
    spec = R6_DOGFOOD_SPECS["h7"]
    baseline_surface = _normalize_text_list(
        baseline_tool_surface or ["mcp:unitares", "hermes"]
    )
    variant_surface = _normalize_text_list(
        variant_tool_surface
        or ["mcp:unitares", "hermes", "mcp:knowledge-graph"]
    )
    return [
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome="baseline-tool-surface-entry",
            response_text=(
                "R6 H7 baseline entry: Hermes performs the bounded task with "
                "the baseline tool surface."
            ),
            model=model,
            memory_context=memory_context,
            locus={"r6_condition": "baseline_tool_surface"},
            tool_surface=baseline_surface,
        ),
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome=spec.default_task_outcome,
            response_text=(
                "R6 H7 variant entry: Hermes performs the same bounded task "
                "with a perturbed tool surface."
            ),
            model=model,
            memory_context=memory_context,
            locus={"r6_condition": "variant_tool_surface"},
            tool_surface=variant_surface,
        ),
        _kg_note_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome=spec.default_task_outcome,
            summary="R6 H7 Hermes tool-surface contrast note",
            details=(
                "Compare the two H7 entries for the same UNITARES identity. "
                "The intended distinction is available tool surface, not "
                "identity lineage."
            ),
            model=model,
            memory_context=memory_context,
            tool_surface=variant_surface,
        ),
    ]


def _build_h8_payloads(
    comparison_key: str,
    *,
    model: str,
    memory_context: str,
    variant_transport: Optional[str],
) -> list[dict[str, Any]]:
    spec = R6_DOGFOOD_SPECS["h8"]
    variant = _clean_text(variant_transport) or "hermes-gateway"
    return [
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome="interactive-transport-entry",
            response_text=(
                "R6 H8 interactive entry: Hermes records the bounded task "
                "through the CLI transport."
            ),
            model=model,
            memory_context=memory_context,
            locus={"r6_condition": "interactive_transport"},
            transport="hermes-cli",
        ),
        _process_update_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome=spec.default_task_outcome,
            response_text=(
                "R6 H8 non-interactive entry: Hermes records the same bounded "
                f"task through {variant}."
            ),
            model=model,
            memory_context=memory_context,
            locus={"r6_condition": "non_interactive_transport"},
            transport=variant,
        ),
        _kg_note_payload(
            comparison_key=comparison_key,
            task_label=spec.title,
            task_outcome=spec.default_task_outcome,
            summary="R6 H8 Hermes transport contrast note",
            details=(
                "Compare the two H8 entries for the same UNITARES identity. "
                "The intended distinction is interactive versus non-interactive "
                "transport, not identity lineage."
            ),
            model=model,
            memory_context=memory_context,
            transport=variant,
        ),
    ]


def _process_update_payload(
    *,
    comparison_key: str,
    task_label: str,
    task_outcome: str,
    response_text: str,
    model: str,
    memory_context: str,
    locus: Mapping[str, Any],
    transport: str = "hermes-cli",
    tool_surface: Any = None,
) -> dict[str, Any]:
    return {
        "name": "process_agent_update",
        "arguments": {
            "response_text": response_text,
            "task_type": "testing",
            "complexity": 0.2,
            "confidence": 0.8,
            "harness_type": "hermes",
            "transport": transport,
            "model_provider": "openai",
            "model": model,
            "memory_context": memory_context,
            "comparison_key": comparison_key,
            "task_label": task_label,
            "task_outcome": task_outcome,
            "tool_surface": _normalize_text_list(
                tool_surface or ["mcp:unitares", "hermes"]
            ),
            "governance_mode": "explicit",
            "verification_source": "harness_self_report",
            "locus": dict(locus),
            "response_mode": "minimal",
        },
    }


def _kg_note_payload(
    *,
    comparison_key: str,
    task_label: str,
    task_outcome: str,
    summary: str,
    details: str,
    model: str,
    memory_context: str,
    transport: str = "hermes-cli",
    tool_surface: Any = None,
) -> dict[str, Any]:
    return {
        "name": "store_knowledge_graph",
        "arguments": {
            "discovery_type": "experiment",
            "summary": summary,
            "details": details,
            "tags": ["r6", "dogfood", "harness-substrate"],
            "harness_type": "hermes",
            "transport": transport,
            "model_provider": "openai",
            "model": model,
            "memory_context": memory_context,
            "comparison_key": comparison_key,
            "task_label": task_label,
            "task_outcome": task_outcome,
            "tool_surface": _normalize_text_list(
                tool_surface or ["mcp:unitares", "hermes"]
            ),
            "governance_mode": "explicit",
            "verification_source": "harness_self_report",
        },
    }


def _assess_h1(entries: Sequence[S22H5Entry]) -> tuple[str, str]:
    if len(entries) < 2:
        return "incomplete", "missing_hermes_model_pair"
    models = {entry.model for entry in entries if entry.model}
    if len(models) < 2:
        return "incomplete", "missing_distinct_model_entries"
    agent_ids = {entry.agent_id for entry in entries if entry.agent_id}
    if not agent_ids:
        return "incomplete", "missing_identity_anchor"
    if len(agent_ids) > 1:
        return "incomplete", "not_same_identity"
    return "complete", "same_identity_distinct_models_observed"


def _assess_h3(entries: Sequence[S22H5Entry]) -> tuple[str, str]:
    if len(entries) < 2:
        return "incomplete", "missing_fresh_identity_pair"
    agent_ids = {entry.agent_id for entry in entries if entry.agent_id}
    if len(agent_ids) < 2:
        return "incomplete", "no_fresh_uuid_pair"
    memory_contexts = {entry.memory_context for entry in entries if entry.memory_context}
    if not memory_contexts:
        return "incomplete", "missing_memory_context"
    if len(memory_contexts) > 1:
        return "incomplete", "memory_context_not_shared"
    return "complete", "fresh_identity_shared_memory_context_observed"


def _assess_h7(entries: Sequence[S22H5Entry]) -> tuple[str, str]:
    if len(entries) < 2:
        return "incomplete", "missing_hermes_tool_surface_pair"
    identity_check = _assess_single_identity(entries)
    if identity_check is not None:
        return identity_check
    surfaces = {
        frozenset(entry.tool_surface)
        for entry in entries
        if entry.tool_surface
    }
    if len(surfaces) < 2:
        return "incomplete", "missing_distinct_tool_surface_entries"
    return "complete", "same_identity_distinct_tool_surfaces_observed"


def _assess_h8(entries: Sequence[S22H5Entry]) -> tuple[str, str]:
    if len(entries) < 2:
        return "incomplete", "missing_hermes_transport_pair"
    identity_check = _assess_single_identity(entries)
    if identity_check is not None:
        return identity_check
    transports = {
        _normalize_transport(entry.transport)
        for entry in entries
        if _normalize_transport(entry.transport)
    }
    if len(transports) < 2:
        return "incomplete", "missing_distinct_transport_entries"
    if not any(_is_interactive_transport(value) for value in transports):
        return "incomplete", "missing_interactive_transport_entry"
    if not any(_is_noninteractive_transport(value) for value in transports):
        return "incomplete", "missing_noninteractive_transport_entry"
    return "complete", "same_identity_interactive_noninteractive_transport_observed"


def _assess_single_identity(entries: Sequence[S22H5Entry]) -> Optional[tuple[str, str]]:
    agent_ids = {entry.agent_id for entry in entries if entry.agent_id}
    if not agent_ids:
        return "incomplete", "missing_identity_anchor"
    if len(agent_ids) > 1:
        return "incomplete", "not_same_identity"
    return None


def _recommendations(experiment_id: str, reason: str) -> list[str]:
    if reason in {
        "same_identity_distinct_models_observed",
        "fresh_identity_shared_memory_context_observed",
        "same_identity_distinct_tool_surfaces_observed",
        "same_identity_interactive_noninteractive_transport_observed",
    }:
        return [f"R6 {experiment_id.upper()} has enough structured dogfood evidence."]
    if reason == "missing_hermes_model_pair":
        return ["Record both baseline and variant Hermes model entries on the same key."]
    if reason == "missing_distinct_model_entries":
        return ["Use two distinct model labels for the H1 Hermes entries."]
    if reason == "not_same_identity":
        return ["Run H1 entries from the same bound UNITARES identity."]
    if reason == "missing_fresh_identity_pair":
        return ["Record a prior Hermes entry and a force_new Hermes entry on the same key."]
    if reason == "no_fresh_uuid_pair":
        return ["Run H3 after onboard(force_new=true) so a fresh UNITARES UUID appears."]
    if reason == "memory_context_not_shared":
        return ["Use the same memory_context label for both H3 entries."]
    if reason == "missing_hermes_tool_surface_pair":
        return ["Record both baseline and perturbed Hermes tool-surface entries."]
    if reason == "missing_distinct_tool_surface_entries":
        return ["Use two distinct non-empty tool_surface lists for H7."]
    if reason == "missing_hermes_transport_pair":
        return ["Record both interactive and non-interactive Hermes transport entries."]
    if reason == "missing_distinct_transport_entries":
        return ["Use two distinct transport labels for H8."]
    if reason == "missing_interactive_transport_entry":
        return ["Record an interactive H8 transport entry, typically hermes-cli."]
    if reason == "missing_noninteractive_transport_entry":
        return ["Record a non-interactive H8 transport entry: gateway, cron, webhook, or dispatch."]
    return ["Record structured Hermes entries with comparison_key and S22 context."]


def _ensure_entry(raw: S22H5Entry | Mapping[str, Any]) -> Optional[S22H5Entry]:
    if isinstance(raw, S22H5Entry):
        return raw
    return normalize_s22_h5_entry(raw)


def _normalize_experiment_id(value: str) -> str:
    text = _clean_text(value)
    if not text:
        raise ValueError("experiment_id is required")
    text = text.lower().replace("_", "-")
    if text not in SUPPORTED_R6_EXPERIMENTS:
        supported = ", ".join(SUPPORTED_R6_EXPERIMENTS)
        raise ValueError(f"experiment_id must be one of: {supported}")
    return text


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items: Any = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        raw_items = (value,)

    items: list[str] = []
    seen = set()
    for item in raw_items:
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return items


def _normalize_transport(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    return text.lower().replace("_", "-").replace(" ", "-")


def _is_interactive_transport(value: str) -> bool:
    return any(token in value for token in ("cli", "stdio", "interactive", "terminal"))


def _is_noninteractive_transport(value: str) -> bool:
    return any(token in value for token in ("gateway", "cron", "webhook", "dispatch"))
