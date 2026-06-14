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
    # This allows ChatGPT and other HTTP clients to get meaningful names
    if client_hint and client_hint != "unknown":
        interface = client_hint
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

