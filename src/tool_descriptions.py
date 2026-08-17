"""Tool descriptions for MCP tool definitions. Loaded from JSON."""
import json
from pathlib import Path

from src.governance_glossary import EISV_INLINE_SUMMARY, render_eisv_glossary

_DESCRIPTIONS_FILE = Path(__file__).parent / "tool_descriptions.json"

_EISV_CLIENT_TOOLS = frozenset({
    "process_agent_update",
    "get_governance_metrics",
    "simulate_update",
    "get_system_history",
    "outcome_event",
    "observe",
    "observe_agent",
    "compare_agents",
    "compare_me_to_similar",
    "detect_anomalies",
    "aggregate_metrics",
})

_IDENTITY_DESCRIPTION_OVERRIDES = {
    "onboard": (
        "Mint your own UNITARES governance identity — the anchor that lets you "
        "read your own state, check in, and recover on your own terms. A fresh "
        "process is a fresh agent: call onboard(force_new=true) and the "
        "identity is yours to use.\n\n"
        "Identity posture (S1-c, 2026-05-23): co-location in a workspace is not "
        "lineage, so the default is to onboard fresh. Declare parent_agent_id "
        "only for a real causal event — a dispatched subagent "
        "(spawn_reason='subagent') or a handoff from an exited prior session "
        "(spawn_reason='explicit'). A succession claim naming a currently-live "
        "agent as parent isn't accepted (lineage_coincidental_rejected): the "
        "live agent is a concurrent sibling, not a predecessor. Registered "
        "dispatched children (subagent, dialectic_reviewer, dispatch) and "
        "compaction continuations legitimately permit a live parent; unknown "
        "reasons receive no exemption. Prefer force_new=true over a "
        "bare onboard() — bare calls can let legacy weak session evidence "
        "pin-resume an unrelated UUID, which isn't the identity you meant to "
        "claim.\n\n"
        "continuity_token is short-lived ownership proof for same-owner "
        "PATH 0 rebinds such as identity(agent_uuid=..., continuity_token=..., "
        "resume=true). It is not a transport-level or cross-process resume "
        "credential — a cross-process onboard(continuity_token=...) won't "
        "resume; use force_new=true plus a parent_agent_id lineage "
        "declaration instead.\n\n"
        "ANTI-PATTERN (for client/harness authors): do not auto-inject "
        "continuity_token between calls at the client transport layer. The "
        "token is per-process-instance proof for the PATH 0 anti-hijack gate "
        "(Identity Honesty Part C, 2026-04-18), not a transport-level identity "
        "claim — replaying it or carrying it into another process re-opens the "
        "silent-resurrection vector Part C closed (any process holding it "
        "could then speak as that agent). For continuity across processes, "
        "declare lineage via parent_agent_id instead.\n\n"
        "Name/model fields are cosmetic/contextual. The returned uuid is your "
        "identity anchor for this process, not a claim that future processes "
        "own it."
    ),
    "identity": (
        "Inspect or re-bind your identity.\n\n"
        "Use identity() with no arguments to see the identity currently bound "
        "to your session. Use identity(name='...') to set a cosmetic display "
        "label.\n\n"
        "To re-bind to a UUID you already own, pass both agent_uuid and a "
        "matching continuity_token: identity(agent_uuid='...', "
        "continuity_token='...', resume=true) — the token is what proves the "
        "UUID is yours. A bare identity(agent_uuid='...', resume=true) is an "
        "unsigned claim on a UUID, so under strict identity mode it reads as "
        "hijack-shaped and won't bind.\n\n"
        "For a fresh process, onboard fresh rather than silently resuming: "
        "call onboard(force_new=true) — a fresh session onboards fresh. "
        "Declare parent_agent_id only for a real causal event — a dispatched "
        "subagent (spawn_reason='subagent') or a handoff from an exited prior "
        "session (spawn_reason='explicit'). Succession claims naming a live "
        "agent as parent aren't accepted; dispatched children and compaction "
        "continuations legitimately permit one.\n\n"
        "ANTI-PATTERN (for client/harness authors): do not auto-inject "
        "continuity_token between calls at the client transport layer. The "
        "token is per-process-instance proof for the PATH 0 anti-hijack gate "
        "(Identity Honesty Part C, 2026-04-18), not a transport-level identity "
        "claim — replaying it or carrying it into another process re-opens the "
        "silent-resurrection vector Part C closed (any process holding it "
        "could then speak as that agent). For continuity across processes, "
        "declare lineage via parent_agent_id instead."
    ),
}

_INFERENCE_DESCRIPTION_OVERRIDES = {
    "list_inference_hosts": (
        "List known inference hosts and live adapter readiness. Read both "
        "available (runtime/config readiness) and accepts_host_id_from "
        "(agent-callable routing). Ollama and Hugging Face route through "
        "call_model; Claude routes through delegate_inference; the Codex host "
        "adapter remains registered but unwired. Listing is available before "
        "onboarding, but inference calls require a bound identity."
    ),
    "describe_inference_host": (
        "Describe one inference host by host_id, including transport, privacy, "
        "cost/accountability classes, model capabilities, runtime availability, "
        "implementation status, and the exact tools that accept it. Discovery "
        "does not invoke the host or validate an answer."
    ),
    "call_model": (
        "Weak/fast synchronous advisory inference through local Ollama or the "
        "configured Hugging Face router (~30s tool ceiling). It returns tool "
        "evidence, not a peer-governance record. For a stronger Claude "
        "consultation use delegate_inference; for an on-record review use "
        "request_review/dialectic(action='request')."
    ),
    "delegate_inference": (
        "Delegate a bounded advisory task to Claude through the operator's "
        "authenticated subscription CLI and the agent-orchestrator. This is "
        "the long-running strong-model lane, separate from call_model. Claude "
        "runs in safe mode with tools disabled and session persistence off. "
        "The result includes prompt/response hashes, exact provider-reported "
        "models_used, usage, cost, latency, requesting identity, and "
        "orchestrator execution ID. Pass model to request an alias or exact "
        "model; omit it to use the operator/CLI default. This creates "
        "attributed evidence but not a dialectic peer-review record. Requires "
        "UNITARES_HOST_ADAPTER_ENABLED=1, AGENT_ORCHESTRATOR_BEARER_TOKEN, and "
        "an authenticated Claude CLI discoverable on PATH, ~/.local/bin, or "
        "UNITARES_CLAUDE_CLI."
    ),
    "dialectic": (
        "Governed review operations: get, list, quick, request, thesis, "
        "antithesis, synthesis, reassign. A request opens an attributed session; "
        "an explicitly assigned or eligible standing peer takes the reviewer "
        "role, otherwise an open slot can be claimed by a first responder or a "
        "flagged orchestrated reviewer process. The operator selects that "
        "orchestrated reviewer's backend with "
        "UNITARES_DIALECTIC_REVIEWER_HOST (local, codex, or claude); backend "
        "failures degrade to local inference and record the fallback. Use "
        "delegate_inference for off-record Claude advice and call_model for "
        "weak/fast synchronous advice."
    ),
}

_DESCRIPTION_APPENDICES = {
    "health_check": (
        "\n\nRESPONSE WRAPPER FIELDS:\n"
        "- server_time: ISO timestamp added by the shared MCP success wrapper\n"
        "- agent_signature: caller identity signature object, e.g. "
        "{\"uuid\": string|null, \"agent_id\": public structured handle, "
        "\"display_name\": cosmetic label, \"identity_context\": "
        "s22.identity_response.v1}; may be {\"uuid\": null} before a caller is bound\n"
        "- _cache: cached health snapshot metadata "
        "(age_seconds, produced_at, stale, probe_interval_seconds, "
        "staleness_threshold_seconds)"
    ),
    "process_agent_update": (
        "\n\nCURRENT HIGH-VALUE PARAMETERS:\n"
        "- response_mode: auto | compact | mirror | full "
        "(aliases: lite->compact, verbose->full; legacy: minimal, standard)\n"
        "- require_strong_identity: reject updates unless identity assurance is strong\n"
        "- recent_tool_results: list of ToolResultEvidence items, shaped as "
        "{tool, summary, is_bad}; kind is inferred when omitted\n"
        "\n\nS22 PROVENANCE FIELDS (optional, descriptive, not identity proof):\n"
        "- provenance_context: preferred object slot for S22 situating metadata; "
        "put harness/model/transport/tool_surface/locus metadata here, not in "
        "recent_tool_results\n"
        "- harness_type / harness: normalized harness family such as "
        "\"codex-cli\", \"claude-code\", or \"hermes\"\n"
        "- model_provider, model, transport, memory_context, tool_surface: "
        "situating metadata for the write\n"
        "- comparison_key, task_label, task_outcome: H5 fields for recording "
        "the same bounded task across harnesses\n"
        "\n"
        "Example H5 provenance fields:\n"
        "{\n"
        "  \"harness_type\": \"codex-cli\",\n"
        "  \"model_provider\": \"openai\",\n"
        "  \"model\": \"gpt-5\",\n"
        "  \"transport\": \"codex-cli\",\n"
        "  \"tool_surface\": [\"terminal\", \"mcp:unitares\"],\n"
        "  \"comparison_key\": \"s22-h5-2026-05-06\",\n"
        "  \"task_label\": \"Run S22 H5 coverage diagnostic\",\n"
        "  \"task_outcome\": \"diagnostic-complete\"\n"
        "}"
    ),
    "outcome_event": (
        "\n\nCURRENT OUTCOME TYPES:\n"
        "- trajectory_validated: server-observed trajectory validation event\n"
        "- dialectic_resolved: dialectic review reached a resolution\n"
        "\n"
        "CURRENT CALIBRATION / PROVENANCE FIELDS:\n"
        "- confidence: agent confidence at outcome time; inferred from last "
        "check-in if omitted\n"
        "- prediction_id: tactical prediction id returned by process_agent_update; "
        "binds this outcome to that prediction\n"
        "- decision_action: decision taken, e.g. proceed or pause\n"
        "- session_id: optional session id; falls back to client_session_id/context\n"
        "- verification_source: agent_reported_tool_result | server_observation | "
        "external_signal\n"
        "- response/detail corroboration metadata: corroboration_grade, "
        "evidence_weight, claim_risk, claimed_fields, verified_fields, "
        "unverified_fields. Agent-reported task_completed summaries with no "
        "independent evidence are claim_only and low-weight."
    ),
}


def _with_eisv_contract(description: str) -> str:
    """Put the field contract in both short and full MCP descriptions."""
    if EISV_INLINE_SUMMARY in description:
        return description
    first_line, separator, remainder = description.partition("\n")
    first_line = f"{first_line.rstrip()} {EISV_INLINE_SUMMARY}"
    description = f"{first_line}{separator}{remainder}" if separator else first_line
    return f"{description}\n\n{render_eisv_glossary()}"


def _load_descriptions() -> dict:
    with open(_DESCRIPTIONS_FILE, encoding="utf-8") as f:
        descriptions = json.load(f)
    # Keep the large legacy JSON stable while overriding fast-moving identity
    # teaching text close to the S1-a implementation.
    descriptions.update(_IDENTITY_DESCRIPTION_OVERRIDES)
    descriptions.update(_INFERENCE_DESCRIPTION_OVERRIDES)
    for tool_name, appendix in _DESCRIPTION_APPENDICES.items():
        if tool_name in descriptions:
            descriptions[tool_name] = f"{descriptions[tool_name]}{appendix}"
    # The legacy JSON is intentionally kept stable, but it previously taught
    # clients that V meant "Void". Normalize that stale example before adding
    # the canonical contract to every EISV-bearing client surface.
    if "process_agent_update" in descriptions:
        descriptions["process_agent_update"] = (
            descriptions["process_agent_update"]
            .replace("Void (V)", "Valence (V)")
            .replace('"V": "Void"', '"V": "Valence"')
        )
    for tool_name in _EISV_CLIENT_TOOLS:
        if tool_name in descriptions:
            descriptions[tool_name] = _with_eisv_contract(descriptions[tool_name])
    return descriptions


TOOL_DESCRIPTIONS = _load_descriptions()


def register_extra_descriptions(descriptions: dict) -> None:
    """Merge plugin-supplied tool descriptions into ``TOOL_DESCRIPTIONS``.

    Called by ``governance_mcp.plugins`` entry-point plugins during
    ``plugin_loader.load_plugins()``. Existing keys are overwritten
    silently — the last loader wins, same precedence as the JSON file.
    """
    TOOL_DESCRIPTIONS.update(descriptions)
