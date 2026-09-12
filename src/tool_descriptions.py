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
        "Mint this process-instance's governance identity and return the "
        "client_session_id to pass on later calls. A fresh process is a fresh "
        "agent: pass force_new=true; a continuity_token without it is "
        "refused, since a token is not a cross-process resume credential. "
        "Declare parent_agent_id only for a real causal event: if the named "
        "parent is still live the call succeeds but the declaration is "
        "silently cleared, unless the child is a dispatched subagent or a "
        "compaction continuation. To attach this session to an identity "
        "minted elsewhere use bind_session. start_session reaches this same "
        "handler and wraps the result in the digest envelope; this canonical "
        "name returns the handler's raw payload unchanged.\n\nIdentity posture (S1-c, "
        "2026-05-23): co-location in a workspace is not lineage, so the "
        "default is to onboard fresh. Declare parent_agent_id only for a real "
        "causal event — a dispatched subagent (spawn_reason='subagent') or a "
        "handoff from an exited prior session (spawn_reason='explicit'). A "
        "succession claim naming a currently-live agent as parent isn't "
        "accepted (lineage_coincidental_rejected): the live agent is a "
        "concurrent sibling, not a predecessor. Registered dispatched "
        "children (subagent, dialectic_reviewer, dispatch) and compaction "
        "continuations legitimately permit a live parent; unknown reasons "
        "receive no exemption. Prefer force_new=true over a bare onboard() — "
        "bare calls can let legacy weak session evidence pin-resume an "
        "unrelated UUID, which isn't the identity you meant to "
        "claim.\n\ncontinuity_token is short-lived ownership proof for "
        "same-owner PATH 0 rebinds such as identity(agent_uuid=..., "
        "continuity_token=..., resume=true). It is not a transport-level or "
        "cross-process resume credential — a cross-process "
        "onboard(continuity_token=...) won't resume; use force_new=true plus "
        "a parent_agent_id lineage declaration instead.\n\nANTI-PATTERN (for "
        "client/harness authors): do not auto-inject continuity_token between "
        "calls at the client transport layer. The token is "
        "per-process-instance proof for the PATH 0 anti-hijack gate (Identity "
        "Honesty Part C, 2026-04-18), not a transport-level identity claim — "
        "replaying it or carrying it into another process re-opens the "
        "silent-resurrection vector Part C closed (any process holding it "
        "could then speak as that agent). For continuity across processes, "
        "declare lineage via parent_agent_id instead.\n\nName/model fields "
        "are cosmetic/contextual. The returned uuid is your identity anchor "
        "for this process, not a claim that future processes own it."
    ),
    "identity": (
        "Resolve which agent this MCP session is bound to, or set a cosmetic "
        "display name. Not a plain read: an argument-less call carries no "
        "proof, so the server infers a binding and may resolve a co-located "
        "agent or mint and persist a new one, marked caller_proven=false — "
        "pass client_session_id to get your own back. name= persists a "
        "cosmetic label only and never looks an agent up. For a fresh process "
        "call onboard(force_new=true). continuity_token is per-process "
        "ownership proof, not a transport-level claim: carrying it into "
        "another process re-opens silent resurrection.\n\nUse identity() "
        "with no arguments to "
        "see the identity currently bound to your session. Use "
        "identity(name='...') to set a cosmetic display label.\n\nTo re-bind "
        "to a UUID you already own, pass both agent_uuid and a matching "
        "continuity_token: identity(agent_uuid='...', continuity_token='...', "
        "resume=true) — the token is what proves the UUID is yours. A bare "
        "identity(agent_uuid='...', resume=true) is an unsigned claim on a "
        "UUID, so under strict identity mode it reads as hijack-shaped and "
        "won't bind.\n\nFor a fresh process, onboard fresh rather than "
        "silently resuming: call onboard(force_new=true) — a fresh session "
        "onboards fresh. Declare parent_agent_id only for a real causal event "
        "— a dispatched subagent (spawn_reason='subagent') or a handoff from "
        "an exited prior session (spawn_reason='explicit'). Succession claims "
        "naming a live agent as parent aren't accepted; dispatched children "
        "and compaction continuations legitimately permit "
        "one.\n\nANTI-PATTERN (for client/harness authors): do not "
        "auto-inject continuity_token between calls at the client transport "
        "layer. The token is per-process-instance proof for the PATH 0 "
        "anti-hijack gate (Identity Honesty Part C, 2026-04-18), not a "
        "transport-level identity claim — replaying it or carrying it into "
        "another process re-opens the silent-resurrection vector Part C "
        "closed (any process holding it could then speak as that agent). For "
        "continuity across processes, declare lineage via parent_agent_id "
        "instead."
    ),
}

_INFERENCE_DESCRIPTION_OVERRIDES = {
    "list_inference_hosts": (
        "List registered inference hosts with live readiness. Read two fields "
        "as different questions: available says the adapter could run (a "
        "cached readiness check, not a promise), accepts_host_id_from says "
        "which tool will take that host as host_id — Ollama and Hugging Face "
        "belong to call_model, the Claude and Codex adapters to "
        "delegate_inference. Listing works before onboarding; inference calls "
        "need a bound identity. Use describe_inference_host for one known id."
    ),
    "describe_inference_host": (
        "Return the registry record for one inference host named by host_id, "
        "including readiness: read accepts_host_id_from, not available, to "
        "learn which tool will take it. An unregistered id returns "
        "INFERENCE_HOST_NOT_FOUND rather than an empty record, so enumerate "
        "ids with list_inference_hosts; call_model or delegate_inference "
        "actually reach the host. Reading the record neither invokes the "
        "host nor proves its credentials work."
    ),
    "call_model": (
        "Run one synchronous advisory completion on the local Ollama lane or "
        "the Hugging Face router, returning tool evidence, never a governed "
        "review record; consult is the better default unless you need this "
        "route control. provider='hf' also needs privacy='cloud' or 'auto', "
        "since the default privacy='local' refuses it — yet that local "
        "default does not screen model ids, so a deepseek-ai/, Qwen/, hf: or "
        "openai/gpt-oss model still routes off-box. host_id rejects the "
        "Claude and Codex adapters; those are delegate_inference's. Requires a "
        "bound identity."
    ),
    "delegate_inference": (
        "Send one bounded prompt to an operator-authorized subscription CLI "
        "(Claude or Codex), spawned as an isolated child with no tools or a "
        "read-only sandbox: it answers, it cannot change anything. It requires a "
        "bound identity and fails closed until the operator sets "
        "UNITARES_HOST_ADAPTER_ENABLED=1 and AGENT_ORCHESTRATOR_BEARER_TOKEN "
        "with the host's authenticated CLI on PATH. On "
        "timeout the child may still be running — the failure carries an "
        "execution id flagged possibly_running, so have it reconciled rather "
        "than reissuing. consult at effort='thorough' takes this same lane "
        "without host controls."
    ),
    "consult": (
        "Primary advisory model-help surface: send a brief, get back advisory "
        "model evidence, never a governed verdict — request_review produces "
        "that. effort='thorough' needs privacy='cloud_allowed'; against the "
        "default privacy='local' it refuses outright unless "
        "allow_degraded=true, which returns a standard local answer instead. "
        "Requires a bound identity. Use call_model or delegate_inference only "
        "for explicit provider, host, model or timeout control."
    ),
    "request_review": (
        "Request governed, on-record judgment through the dialectic lifecycle. "
        "The response identifies the actual reviewer path and provenance. This "
        "is categorically different from consult, which returns advisory model "
        "evidence only."
    ),
    "dialectic": (
        "Open and advance governed, on-record peer review sessions. get and "
        "list serve unbound callers; every other action needs a bound "
        "identity, quick fails without issue_description, and thesis, "
        "antithesis, synthesis and reassign each need a session_id the "
        "schema does not mark required. request refuses "
        "with SESSION_EXISTS while your agent already has an active session. "
        "For a bound caller get with check_timeout=true becomes a write that "
        "can flag the session for facilitation or flip its phase to FAILED. "
        "UNITARES_DIALECTIC_REVIEWER_HOST picks the orchestrated reviewer "
        "backend (local, codex or claude); a failure there degrades to "
        "local inference and records the fallback. request_review is the "
        "one-call alias for request; consult advises without opening a "
        "record."
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
        "- response_mode: auto | compact | standard | mirror | full "
        "(interpreted->standard bounded summary; lite->compact; verbose->full; "
        "legacy bare shape: minimal)\n"
        "- require_strong_identity: reject updates unless identity assurance is strong\n"
        "- recent_tool_results: list of ToolResultEvidence items, shaped as "
        "{tool, summary, is_bad}; kind is inferred when omitted\n"
        "\n\nS22 PROVENANCE FIELDS (optional, descriptive, not identity proof):\n"
        "- provenance_context: preferred object slot for S22 situating metadata, "
        "including versioned runtime_provenance from harness adapters; "
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
