"""
Helper functions for generating meaningful agent names and onboarding suggestions.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import os
import re
def detect_interface_context() -> Dict[str, str]:
    """
    Detect interface and context information for name generation.
    
    Returns:
        dict with interface, model hints, and other context
    """
    context = {
        "interface": "mcp_client",
        "model_hint": None,
        "environment": None,
        "purpose_hint": None
    }
    
    # Check for explicit override
    if os.getenv("GOVERNANCE_AGENT_PREFIX"):
        context["interface"] = os.getenv("GOVERNANCE_AGENT_PREFIX")
    
    # Detect interface
    if os.getenv("CURSOR_PID") or os.getenv("CURSOR_VERSION"):
        context["interface"] = "cursor"
    elif os.getenv("VSCODE_PID"):
        context["interface"] = "vscode"
    elif os.getenv("CLAUDE_DESKTOP"):
        context["interface"] = "claude_desktop"
    
    # Detect model hints from environment
    if os.getenv("OPENAI_API_KEY"):
        context["model_hint"] = "gpt"
    elif os.getenv("ANTHROPIC_API_KEY"):
        context["model_hint"] = "claude"
    elif os.getenv("GOOGLE_AI_API_KEY") or os.getenv("GEMINI_API_KEY"):
        context["model_hint"] = "gemini"
    
    # Detect environment
    if os.getenv("CI"):
        context["environment"] = "ci"
    elif os.getenv("TEST"):
        context["environment"] = "test"
    
    return context

def generate_name_suggestions(
    context: Optional[Dict[str, str]] = None,
    purpose: Optional[str] = None,
    existing_names: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Generate meaningful name suggestions based on context.
    
    Args:
        context: Interface/model context (from detect_interface_context)
        purpose: Optional purpose/work description
        existing_names: List of existing agent names to avoid collisions
    
    Returns:
        List of suggestion dicts with name, description, and rationale
    """
    if context is None:
        context = detect_interface_context()
    
    suggestions = []
    timestamp = datetime.now().strftime("%Y%m%d")
    time_short = datetime.now().strftime("%Y%m%d_%H%M")
    
    interface = context.get("interface", "mcp")
    model_hint = context.get("model_hint")
    environment = context.get("environment")
    
    # Build base components
    base_parts = []
    if environment:
        base_parts.append(environment)
    if interface != "mcp_client":
        base_parts.append(interface.replace("_", "-"))
    if model_hint:
        base_parts.append(model_hint)
    
    # Suggestion 1: Context-based with purpose
    if purpose:
        purpose_clean = re.sub(r'[^a-z0-9_-]', '', purpose.lower().replace(' ', '_'))[:20]
        name1 = f"{purpose_clean}_{interface}_{timestamp}"
        suggestions.append({
            "name": name1,
            "description": f"Purpose-based: {purpose}",
            "rationale": "Includes your work purpose for easy identification",
            "example": f"e.g., '{name1}'"
        })
    
    # Suggestion 2: Interface + model + date (if model detected)
    if model_hint:
        name2 = f"{interface}_{model_hint}_{timestamp}"
        suggestions.append({
            "name": name2,
            "description": f"Interface + model: {interface} with {model_hint}",
            "rationale": "Clear identification of your environment and model",
            "example": f"e.g., '{name2}'"
        })
    
    # Suggestion 3: Session-based with timestamp
    name3 = f"{interface}_session_{time_short}"
    suggestions.append({
        "name": name3,
        "description": "Session-based with precise timestamp",
        "rationale": "Unique per session, easy to find chronologically",
        "example": f"e.g., '{name3}'"
    })
    
    # Suggestion 4: Simple interface + date (if no model detected, or as alternative)
    if not model_hint or len(suggestions) < 4:
        name4 = f"{interface}_{timestamp}"
        suggestions.append({
            "name": name4,
            "description": f"Simple: {interface} with date",
            "rationale": "Clean, simple identifier",
            "example": f"e.g., '{name4}'"
        })
    
    # Check for collisions and adjust
    if existing_names:
        existing_set = set(existing_names)
        for sug in suggestions:
            if sug["name"] in existing_set:
                # Add suffix to make unique
                counter = 1
                original = sug["name"]
                while f"{original}_{counter}" in existing_set:
                    counter += 1
                sug["name"] = f"{original}_{counter}"
                sug["note"] = "Adjusted for uniqueness"
    
    return suggestions[:4]  # Return top 4 suggestions

_MAX_HINT_LEN = 40


def _safe_interface_hint(client_hint: Optional[str]) -> Optional[str]:
    """Vet & sanitize a caller-supplied client_hint for use as the leading
    interface token of a structured id.

    client_hint is free text from the caller. Two failure modes must be
    contained before it can seed an id (KG dogfood 2026-05-09 "client_hint
    leaks into agent_id namespace"; follow-up 2026-06-10):

    1. Shape — a descriptor like ``"Anthropic Claude, mobile app, dogfooding"``
       must not become an identifier full of spaces and commas. Strip to
       ``[a-zA-Z0-9_-]`` (mirrors validators.sanitize_agent_name), collapse
       separators, and length-cap.
    2. Namespace — the leading token is immediately followed by ``_`` in the
       id, so a hint equal to a reserved-prefix root (``mcp``, ``admin``,
       ``root``, ``system``, ``governance``, ``auth``) or any reserved name
       would mint a reserved-prefix agent_id that downstream validation
       refuses.

    Returns a sanitized, identifier-shaped token, or ``None`` when the hint is
    empty/``"unknown"``, sanitizes to nothing, or collides with the reserved
    namespace. The caller falls back to the detected interface on ``None``.
    """
    if not client_hint or client_hint == "unknown":
        return None
    from ..validators import RESERVED_NAMES, RESERVED_PREFIXES

    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '_', client_hint)
    cleaned = re.sub(r'_+', '_', cleaned).strip('_-')[:_MAX_HINT_LEN].strip('_-')
    if not cleaned:
        return None

    reserved_roots = {prefix.rstrip("_") for prefix in RESERVED_PREFIXES}
    cleaned_lower = cleaned.lower()
    leading_token = cleaned_lower.replace("-", "_").split("_", 1)[0]
    if cleaned_lower in RESERVED_NAMES or leading_token in reserved_roots:
        return None
    return cleaned


def generate_structured_id(
    context: Optional[Dict[str, str]] = None,
    existing_ids: Optional[List[str]] = None,
    client_hint: Optional[str] = None,
    model_type: Optional[str] = None,
    agent_uuid: Optional[str] = None
) -> str:
    """
    Generate a structured auto-id for an agent.

    This is the "agent_id" tier in the three-tier identity model:
    - UUID (immutable) - technical identifier
    - agent_id (structured) - this function, auto-generated
    - display_name (nickname) - user-chosen via identity(name=...)

    Format: {interface}_{model}_{date}_{uuid8} e.g.,
        "chatgpt_claude_20251226_a4be406c"
    Or without model: {interface}_{date}_{uuid8} e.g.,
        "cursor_20251226_a4be406c"

    The {interface}_{model}_{date} prefix stays greppable/bucketable; the
    uuid8 fragment (first block of agent_uuid, matching the existing label
    convention) makes the id structurally unique per agent. Without it,
    every same-model/same-day mint collapses onto one bucket label — and
    the legacy collision-counter below only ever saw in-memory metadata, so
    the suffix never fired across the persisted registry and produced
    *exact* repeats (e.g. dozens of "Claude_20260613" sharing one id
    string). Residents pin their structured id explicitly and never flow
    through here, so their cross-restart continuity is unaffected.

    When agent_uuid is omitted, falls back to the legacy collision-counter
    suffix ("cursor_20251226_2") for callers that have no uuid in scope.

    Args:
        context: Interface context (from detect_interface_context)
        existing_ids: List of existing structured IDs to avoid collisions
        client_hint: Optional explicit client type (e.g., "chatgpt", "cursor")
                     Takes precedence over auto-detected interface
        model_type: Optional model identifier (e.g., "claude", "gemini", "gpt4")
                    When provided, creates distinct identity per model
        agent_uuid: Optional agent UUID. When provided, its first block is
                    appended as a uuid8 fragment so the id is unique per
                    agent rather than a per-day bucket label.

    Returns:
        Unique structured ID string
    """
    if context is None:
        context = detect_interface_context()

    timestamp = datetime.now().strftime("%Y%m%d")

    # Use client_hint if provided (takes precedence over auto-detection)
    # This allows ChatGPT and other HTTP clients to get meaningful names.
    # client_hint is free text from the caller, so it must NOT be allowed to
    # seed the leading token of the structured id with a reserved/privileged
    # word (e.g. "admin", "mcp", "root"): that produces a reserved-prefix
    # agent_id which is later rejected by validate_agent_id_reserved_names,
    # leaking free text into the privileged identifier namespace (KG dogfood
    # 2026-05-09 "client_hint leaks into agent_id namespace"). When the hint
    # would collide, fall back to the detected interface instead.
    safe_hint = _safe_interface_hint(client_hint)
    if safe_hint:
        interface = safe_hint
    else:
        interface = context.get("interface", "mcp")

    # Normalize interface name (remove _client suffix, use underscores)
    interface = interface.replace("_client", "").replace("-", "_")

    # Include model type if provided (creates distinct identity per model)
    if model_type:
        model = model_type.lower().replace("-", "_").replace(".", "_")
        # Simplify common model names
        if "claude" in model:
            model = "claude"
        elif "gemini" in model:
            model = "gemini"
        elif "gpt" in model:
            model = "gpt"
        elif "llama" in model:
            model = "llama"
        base_id = f"{interface}_{model}_{timestamp}"
    else:
        base_id = f"{interface}_{timestamp}"

    # Append a uuid8 fragment when we have the agent's UUID. This is what
    # makes the id unique per agent instead of a per-(interface,model,day)
    # bucket; the fragment is the first UUID block (same convention as
    # agent labels, e.g. "...claude_a4be406c"). A uuid-suffixed id is
    # already unique, so the collision-counter below is a no-op for it and
    # only serves the legacy (agent_uuid=None) callers.
    if agent_uuid:
        fragment = str(agent_uuid).split("-")[0].lower()
        if fragment:
            base_id = f"{base_id}_{fragment}"

    # Check for collisions
    if existing_ids:
        existing_set = set(existing_ids)
        if base_id not in existing_set:
            return base_id

        # Find unique suffix
        counter = 2
        while f"{base_id}_{counter}" in existing_set:
            counter += 1
        return f"{base_id}_{counter}"

    return base_id


def _uuid8_fragment(agent_uuid: Optional[str]) -> str:
    """Return the first UUID block as a lowercase hex fragment, or ``""``.

    Guards against non-UUID identifiers — test placeholders like ``agent-1``,
    structured ids, or model names — that would otherwise contribute a garbage
    suffix such as ``_agent``. Tolerates the ``agent-<hex>`` redacted form by
    stripping that prefix before reading the first block. A canonical UUID's
    first block is exactly 8 hex digits, so anything else yields no fragment.
    """
    if not agent_uuid:
        return ""
    raw = str(agent_uuid)
    if raw.startswith("agent-"):
        raw = raw[len("agent-"):]
    candidate = raw.split("-")[0][:8].lower()
    if len(candidate) == 8 and all(c in "0123456789abcdef" for c in candidate):
        return candidate
    return ""


def disambiguate_public_handle(
    public_agent_id: Optional[str],
    structured_id: Optional[str] = None,
    agent_uuid: Optional[str] = None,
) -> Optional[str]:
    """Return a per-agent-unique display handle for registry/dashboard views.

    ``public_agent_id`` is the ``{Model}_{date}`` bucket form minted by
    ``identity.resolution._generate_agent_id``. It carries no uniqueness
    suffix, so every same-model/same-day mint collapses onto one label and
    genuinely distinct agents (distinct UUIDs) render identically on the
    dashboard — the apparent "agent duplication". This appends the agent's
    uuid8 fragment (the first UUID block, the same convention
    ``generate_structured_id`` and agent labels already use) so the bucket
    prefix stays greppable while the handle is unique per agent. It covers
    legacy rows that predate the unique ``structured_id`` too, since the
    suffix derives from ``agent_uuid`` rather than from a stored field.

    The fragment is only appended when ``agent_uuid`` is genuinely UUID-like
    (see ``_uuid8_fragment``); a missing or non-UUID identifier leaves the
    handle unchanged rather than tacking on a garbage suffix.

    Preference: disambiguate ``public_agent_id`` when present (keeping the
    readable capitalized bucket form); else fall back to ``structured_id``
    (already uuid8-suffixed by ``generate_structured_id``). Returns ``None``
    when no base handle is available so callers keep their own fallbacks.
    """
    fragment = _uuid8_fragment(agent_uuid)

    if public_agent_id:
        base = str(public_agent_id)
        if fragment and not base.lower().endswith(fragment):
            return f"{base}_{fragment}"
        return base
    if structured_id:
        return str(structured_id)
    return None


def format_naming_guidance(
    suggestions: List[Dict[str, Any]],
    current_uuid: Optional[str] = None
) -> Dict[str, Any]:
    """
    Format naming guidance for agent response.
    
    Args:
        suggestions: List of name suggestions
        current_uuid: Current agent UUID (for reference)
    
    Returns:
        Formatted guidance dict
    """
    guidance = {
        "message": "Choose a meaningful name to help identify your work",
        "convention": "{purpose}_{interface}_{date} or {interface}_{model}_{date}",
        "suggestions": suggestions,
        "how_to": "Call identity(name='your_chosen_name') to set your name",
        "examples": [
            "feedback_governance_20251221",
            "cursor_claude_20251221",
            "debug_session_20251221_1430",
            "exploration_mcp_20251221"
        ],
        "tips": [
            "Include purpose/work type for easy identification",
            "Add interface/model if working in specific environment",
            "Use date for chronological organization",
            "Keep it concise but descriptive (20-40 chars recommended)"
        ]
    }
    
    if current_uuid:
        guidance["current_uuid"] = current_uuid[:16] + "..."
        guidance["note"] = "You're currently identified by UUID. Naming yourself makes it easier to find your work later."
    
    return guidance

