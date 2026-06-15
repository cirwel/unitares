"""Tool descriptions for MCP tool definitions. Loaded from JSON."""
import json
from pathlib import Path

_DESCRIPTIONS_FILE = Path(__file__).parent / "tool_descriptions.json"

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
        "(spawn_reason='explicit'). Naming a currently-live agent as parent "
        "isn't accepted (lineage_coincidental_rejected): a live agent is a "
        "concurrent sibling, not a predecessor. Prefer force_new=true over a "
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
        "session (spawn_reason='explicit'); naming a live agent as parent "
        "isn't accepted.\n\n"
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

_DESCRIPTION_APPENDICES = {
    "health_check": (
        "\n\nRESPONSE WRAPPER FIELDS:\n"
        "- server_time: ISO timestamp added by the shared MCP success wrapper\n"
        "- agent_signature: caller identity signature object, e.g. "
        "{\"uuid\": string|null}; may be {\"uuid\": null} before a caller is bound\n"
        "- _cache: cached health snapshot metadata "
        "(age_seconds, produced_at, stale, probe_interval_seconds, "
        "staleness_threshold_seconds)"
    ),
    "process_agent_update": (
        "\n\nCURRENT HIGH-VALUE PARAMETERS:\n"
        "- response_mode: minimal | compact | standard | full | mirror | auto\n"
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


def _load_descriptions() -> dict:
    with open(_DESCRIPTIONS_FILE, encoding="utf-8") as f:
        descriptions = json.load(f)
    # Keep the large legacy JSON stable while overriding fast-moving identity
    # teaching text close to the S1-a implementation.
    descriptions.update(_IDENTITY_DESCRIPTION_OVERRIDES)
    for tool_name, appendix in _DESCRIPTION_APPENDICES.items():
        if tool_name in descriptions:
            descriptions[tool_name] = f"{descriptions[tool_name]}{appendix}"
    return descriptions


TOOL_DESCRIPTIONS = _load_descriptions()


def register_extra_descriptions(descriptions: dict) -> None:
    """Merge plugin-supplied tool descriptions into ``TOOL_DESCRIPTIONS``.

    Called by ``governance_mcp.plugins`` entry-point plugins during
    ``plugin_loader.load_plugins()``. Existing keys are overwritten
    silently — the last loader wins, same precedence as the JSON file.
    """
    TOOL_DESCRIPTIONS.update(descriptions)
