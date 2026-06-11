"""
Pattern detection helpers for tool handlers.

Detects code changes and prompts for testing.
"""

from typing import Dict, Any, Optional
from src.pattern_tracker import get_pattern_tracker
from src.logging_utils import get_logger
logger = get_logger(__name__)

def detect_code_changes(tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Detect if a tool call involves code changes that should be tested.
    
    Returns:
        Change info if detected, None otherwise
    """
    # Tools that modify code
    code_change_tools = {
        "search_replace": ["file_path"],
        "write": ["file_path"],
        "edit_notebook": ["target_notebook"],
    }
    
    if tool_name not in code_change_tools:
        return None
    
    # Extract file paths
    file_paths = []
    for key in code_change_tools[tool_name]:
        if key in arguments:
            value = arguments[key]
            if isinstance(value, str):
                file_paths.append(value)
            elif isinstance(value, list):
                file_paths.extend(value)
    
    if not file_paths:
        return None
    
    # Filter to code files
    code_extensions = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cpp", ".c", ".h"}
    code_files = [f for f in file_paths if any(f.endswith(ext) for ext in code_extensions)]
    
    if not code_files:
        return None
    
    return {
        "change_type": "code_edit",
        "files_changed": code_files,
        "tool": tool_name
    }

def record_hypothesis_if_needed(agent_id: str, tool_name: str, arguments: Dict[str, Any]) -> None:
    """Record a hypothesis if the tool call involves code changes."""
    change_info = detect_code_changes(tool_name, arguments)
    if change_info:
        tracker = get_pattern_tracker()
        tracker.record_hypothesis(
            agent_id=agent_id,
            change_type=change_info["change_type"],
            files_changed=change_info["files_changed"],
            hypothesis=f"Made changes via {change_info['tool']}"
        )
        logger.debug(f"[PATTERN_TRACKING] Recorded hypothesis for {agent_id[:8]}...: {change_info['files_changed']}")

def check_untested_hypotheses(agent_id: str) -> Optional[str]:
    """
    Check if agent has untested hypotheses and return warning message.
    
    Returns:
        Warning message if untested hypotheses exist, None otherwise
    """
    tracker = get_pattern_tracker()
    warning = tracker.check_untested_hypotheses(agent_id, max_minutes=5)
    if warning:
        return warning["message"]
    return None

def mark_hypothesis_tested(agent_id: str, tool_name: str, arguments: Dict[str, Any]) -> None:
    """
    Mark hypotheses as tested if tool call involves testing/verification.
    
    Testing tools: run_terminal_cmd (with test/check/verify), test-related tools
    """
    # Tools that indicate testing
    testing_indicators = ["test", "check", "verify", "validate", "run"]
    
    # Check if tool name or arguments suggest testing
    is_testing = any(indicator in tool_name.lower() for indicator in testing_indicators)
    
    # Check arguments for test-related content
    if not is_testing:
        args_str = str(arguments).lower()
        is_testing = any(indicator in args_str for indicator in testing_indicators)
    
    if is_testing:
        # Extract file paths from arguments (might be testing related files)
        file_paths = []
        for key in ["file_path", "target_file", "path", "file"]:
            if key in arguments:
                value = arguments[key]
                if isinstance(value, str):
                    file_paths.append(value)
                elif isinstance(value, list):
                    file_paths.extend(value)
        
        if file_paths:
            tracker = get_pattern_tracker()
            tracker.mark_hypothesis_tested(agent_id, file_paths)
            logger.debug(f"[PATTERN_TRACKING] Marked hypotheses as tested for {agent_id[:8]}...")

