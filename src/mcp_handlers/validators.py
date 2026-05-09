"""
Parameter validation helpers for MCP tool handlers.

Provides consistent validation for common parameter types (enums, ranges, formats)
with helpful error messages for agents.

LITE MODEL SUPPORT:
- validate_and_coerce_params(): Smart validation that fixes common mistakes
- Helpful error messages guide smaller models on correct formatting
"""
from typing import Dict, Any, Optional, Tuple, List
from mcp.types import TextContent
from .utils import error_response
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.logging_utils import get_logger

logger = get_logger(__name__)
PARAM_ALIASES: Dict[str, Dict[str, str]] = {'store_knowledge_graph': {'discovery': 'summary', 'insight': 'summary', 'finding': 'summary', 'content': 'details', 'text': 'summary', 'message': 'summary', 'note': 'summary', 'learning': 'summary', 'observation': 'summary', 'type': 'discovery_type', 'kind': 'discovery_type', 'category': 'discovery_type'}, 'leave_note': {'discovery': 'summary', 'insight': 'summary', 'finding': 'summary', 'text': 'summary', 'note': 'summary', 'content': 'summary', 'message': 'summary', 'learning': 'summary'}, 'search_knowledge_graph': {'search': 'query', 'term': 'query', 'text': 'query', 'find': 'query'}, 'process_agent_update': {'text': 'response_text', 'message': 'response_text', 'update': 'response_text', 'content': 'response_text', 'work': 'response_text', 'summary': 'response_text'}, 'identity': {'label': 'name', 'display_name': 'name', 'nickname': 'name'}, 'agent': {'op': 'action'}}

def apply_param_aliases(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Apply parameter aliases - convert intuitive names to canonical ones.

    Collision policy: when both an alias and its canonical key are present,
    the canonical value wins and the alias is dropped (warning logged if the
    values differ). The previous one-pass implementation let dict-iteration
    order decide, which silently clobbered an explicit canonical value —
    e.g. leave_note(summary=..., content=...) overwrote summary with content
    because content aliases to summary.
    """
    aliases = PARAM_ALIASES.get(tool_name)
    if not aliases:
        return arguments
    result: Dict[str, Any] = {}
    # Pass 1: copy non-aliased keys verbatim. Canonical values supplied
    # directly land here untouched regardless of dict iteration order.
    for key, value in arguments.items():
        if key not in aliases:
            result[key] = value
    # Pass 2: apply aliases only where the canonical slot is unfilled.
    for key, value in arguments.items():
        if key not in aliases:
            continue
        canonical = aliases[key]
        if canonical in result:
            if result[canonical] != value:
                # canonical is sourced from the static PARAM_ALIASES values
                # (no taint from arguments). The aliased key itself is not
                # logged — caller-supplied keys could in principle contain
                # sensitive identifiers, and CodeQL's clear-text-logging
                # rule (correctly) treats arguments.items() as tainted.
                logger.warning(
                    "apply_param_aliases(%r): an alias collided with canonical %r; "
                    "keeping canonical, dropping alias value",
                    tool_name, canonical,
                )
            continue
        result[canonical] = value
    return result
# Legacy generic validators removed (Pydantic schemas now handle types, enums, and bounds)

def validate_file_path_policy(file_path: str) -> Tuple[Optional[str], Optional[TextContent]]:
    """
    Validate file path against project policies (anti-proliferation).

    POLICIES:
    1. Test scripts must be in tests/ directory to prevent proliferation.
    2. Markdown files in migration target directories should use knowledge graph instead.

    Args:
        file_path: Path to validate

    Returns:
        Tuple of (warning_message, None) if violation detected, (None, None) if OK.
        Returns warning, not error, to inform but not block.
    """
    import os
    from pathlib import Path
    if file_path is None:
        return (None, None)
    file_path = os.path.normpath(file_path)
    basename = os.path.basename(file_path)
    dirname = os.path.dirname(file_path)
    path_parts = file_path.split(os.sep)
    if (basename.startswith('test_') or basename.startswith('demo_')) and basename.endswith('.py'):
        if not dirname.endswith('tests') and 'tests' not in dirname.split(os.sep):
            warning = f"⚠️ POLICY VIOLATION: Test script '{basename}' should be in 'tests/' directory.\nLocation: {file_path}\nPolicy: All test_*.py and demo_*.py files must be in tests/ to prevent proliferation.\nAction: Move this file to tests/ directory or rename it."
            return (warning, None)
    if basename.endswith('.md'):
        APPROVED_FILES = {
            'README.md',
            'docs/CHANGELOG.md',
            'docs/UNIFIED_ARCHITECTURE.md',
            'docs/guides/START_HERE.md',
            'docs/guides/TROUBLESHOOTING.md',
            'docs/guides/CIRS_PROTOCOL.md',
            'docs/operations/DEFINITIVE_PORTS.md',
            'docs/operations/OPERATOR_RUNBOOK.md',
            'docs/operations/database_architecture.md',
            'docs/operations/contract-drift-playbook.md',
            'docs/dev/CANONICAL_SOURCES.md',
            'docs/dev/CIRCUIT_BREAKER_DIALECTIC.md',
            'docs/dev/TOOL_REGISTRATION.md',
            'docs/dev/validation-roadmap.md',
            'scripts/README.md',
            'data/README.md',
            'tools/README.md',
        }
        MIGRATION_TARGET_DIRS = {'analysis', 'fixes', 'reflection', 'proposals'}
        if 'docs' in path_parts:
            docs_index = path_parts.index('docs')
            if docs_index + 1 < len(path_parts):
                subdir = path_parts[docs_index + 1]
                if subdir in MIGRATION_TARGET_DIRS:
                    rel_path = os.path.relpath(file_path, os.getcwd()) if os.path.isabs(file_path) else file_path
                    if rel_path not in APPROVED_FILES:
                        warning = f"⚠️ POLICY VIOLATION: Markdown file in migration target directory.\nLocation: {file_path}\nPolicy: Files in docs/{subdir}/ should use store_knowledge_graph() instead of creating markdown files.\nAction: Use store_knowledge_graph() for insights/discoveries, or consolidate into existing approved docs.\nApproved files: {', '.join(sorted(APPROVED_FILES))}"
                        return (warning, None)
        rel_path = os.path.relpath(file_path, os.getcwd()) if os.path.isabs(file_path) else file_path
        if rel_path not in APPROVED_FILES:
            if 'docs' in path_parts:
                docs_index = path_parts.index('docs')
                if docs_index + 1 < len(path_parts):
                    subdir = path_parts[docs_index + 1]
                    if subdir not in {'guides', 'reference', 'archive', 'operations', 'dev', 'engineering', 'meta'}:
                        warning = f"⚠️ POLICY WARNING: New markdown file not on approved list.\nLocation: {file_path}\nPolicy: New markdown files should be ≥500 words and on approved list, or use store_knowledge_graph() instead.\nAction: Consider using store_knowledge_graph() for insights, or ensure file is ≥500 words and consolidate into existing docs.\nApproved files: {', '.join(sorted(APPROVED_FILES))}"
                        return (warning, None)
    return (None, None)

def sanitize_agent_name(agent_id: str) -> str:
    """Strip invalid characters from agent_id, keeping only [a-zA-Z0-9_-]."""
    import re
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_id)
    # Collapse multiple underscores and strip leading/trailing
    sanitized = re.sub(r'_+', '_', sanitized).strip('_-')
    # Ensure minimum length
    if len(sanitized) < 3:
        sanitized = sanitized + '_agent'
    return sanitized

def validate_agent_id_format(agent_id: str) -> Tuple[Optional[str], Optional[TextContent]]:
    """
    Validate and sanitize agent_id format for safety (filesystem, URLs, etc).

    UX FIX (Dec 2025): Auto-sanitize instead of failing.
    Bad names get fixed, not rejected. Never returns an error.

    Args:
        agent_id: Agent ID to validate/sanitize

    Returns:
        Tuple of (sanitized_id, None) - always succeeds.
    """
    sanitized = sanitize_agent_name(agent_id)
    return (sanitized, None)

def validate_agent_id_reserved_names(agent_id: str) -> Tuple[Optional[str], Optional[TextContent]]:
    """
    Validate agent_id against reserved/privileged names.

    SECURITY: Block privileged names that could cause confusion or privilege escalation.

    Args:
        agent_id: Agent ID to validate

    Returns:
        Tuple of (None, error_response) if reserved name detected, (agent_id, None) if OK.
    """
    if agent_id is None:
        return (None, None)
    agent_id_lower = agent_id.lower()
    RESERVED_NAMES = {'system', 'admin', 'root', 'superuser', 'administrator', 'sudo', 'null', 'undefined', 'none', 'anonymous', 'guest', 'default', 'mcp', 'server', 'client', 'handler', 'transport', 'governance', 'monitor', 'arbiter', 'validator', 'auditor', 'security', 'auth', 'identity', 'certificate'}
    RESERVED_PREFIXES = ('system_', 'admin_', 'root_', 'mcp_', 'governance_', 'auth_')
    if agent_id_lower in RESERVED_NAMES:
        return (None, error_response(f"SECURITY: agent_id '{agent_id}' is reserved for system use", details={'error_type': 'reserved_agent_id', 'reason': 'Reserved name blocked to prevent privilege confusion'}, recovery={'action': 'Choose a different agent_id that describes your work', 'example': 'my_agent_work_20251209', 'note': 'Reserved names include: system, admin, root, null, etc.'}))
    if agent_id_lower.startswith(RESERVED_PREFIXES):
        return (None, error_response(f"SECURITY: agent_id '{agent_id}' uses reserved prefix", details={'error_type': 'reserved_prefix', 'reason': 'Reserved prefixes blocked to prevent privilege confusion'}, recovery={'action': 'Choose an agent_id without system/admin/governance prefixes', 'example': 'my_agent_work_20251209'}))
    return (agent_id, None)

def validate_agent_id_policy(agent_id: str) -> Tuple[Optional[str], Optional[TextContent]]:
    """Policy check on agent_id (reserved names only).

    Returns (warning_string, None) if concern, (None, None) if OK.
    Never blocks — warnings are advisory.
    """
    if not agent_id:
        return (None, None)
    _, err = validate_agent_id_reserved_names(agent_id)
    if err:
        return ("Agent ID uses a reserved name", None)
    return (None, None)

def detect_script_creation_avoidance(response_text: str) -> List[str]:
    """Detect patterns in response text that suggest test/script avoidance.

    Returns list of warning strings (empty if no concerns).
    """
    if not response_text:
        return []
    warnings = []
    avoidance_phrases = [
        "skipping tests",
        "no tests needed",
        "tests not necessary",
        "skip test creation",
    ]
    lower = response_text.lower()
    for phrase in avoidance_phrases:
        if phrase in lower:
            warnings.append(f"Possible test avoidance detected: '{phrase}'")
    return warnings
