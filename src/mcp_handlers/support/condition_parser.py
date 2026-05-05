"""
Condition Parser for Dialectic Resolutions

Parses natural language conditions into structured format and applies them.
"""

import re
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
logger = get_logger(__name__)

class ParsedCondition:
    """Structured representation of a parsed condition"""
    
    def __init__(self, action: str, target: str, value: Optional[Any] = None, unit: Optional[str] = None):
        self.action = action  # "set", "reduce", "increase", "monitor", "limit"
        self.target = target  # "complexity", "risk", "coherence", "monitoring_duration"
        self.value = value  # Numeric value or None
        self.unit = unit  # "hours", "minutes", "percent", etc.
        self.original = ""  # Original condition string
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "value": self.value,
            "unit": self.unit,
            "original": self.original
        }

def parse_condition(condition: str) -> ParsedCondition:
    """
    Parse a natural language condition into structured format.
    
    Examples:
        "Reduce complexity to 0.3" → {"action": "reduce", "target": "complexity", "value": 0.3}
        "Monitor for 24h" → {"action": "monitor", "target": "monitoring_duration", "value": 24, "unit": "hours"}
        "Set risk threshold to 0.4" → {"action": "set", "target": "risk_threshold", "value": 0.4}
    
    Args:
        condition: Natural language condition string
    
    Returns:
        ParsedCondition object
    """
    condition_lower = condition.lower().strip()
    parsed = ParsedCondition(action="unknown", target="unknown")
    parsed.original = condition
    
    # Pattern 1: "Reduce/Increase/Set X to Y"
    pattern1 = re.compile(r'(reduce|increase|set|lower|raise)\s+(\w+)\s+to\s+([\d.]+)', re.IGNORECASE)
    match1 = pattern1.search(condition)
    if match1:
        action = match1.group(1).lower()
        target = match1.group(2).lower()
        value = float(match1.group(3))
        
        # Normalize action
        if action in ["reduce", "lower"]:
            parsed.action = "reduce"
        elif action in ["increase", "raise"]:
            parsed.action = "increase"
        else:
            parsed.action = "set"
        
        # Normalize target
        parsed.target = _normalize_target(target)
        parsed.value = value
        return parsed
    
    # Pattern 2: "Monitor for X hours/minutes"
    pattern2 = re.compile(r'monitor\s+for\s+([\d.]+)\s*(hours?|minutes?|h|m)', re.IGNORECASE)
    match2 = pattern2.search(condition)
    if match2:
        value = float(match2.group(1))
        unit = match2.group(2).lower()
        if unit.startswith('h'):
            unit = "hours"
        elif unit.startswith('m'):
            unit = "minutes"
        
        parsed.action = "monitor"
        parsed.target = "monitoring_duration"
        parsed.value = value
        parsed.unit = unit
        return parsed
    
    # Pattern 3: "Keep X below/above Y"
    pattern3 = re.compile(r'keep\s+(\w+)\s+(below|above|under|over)\s+([\d.]+)', re.IGNORECASE)
    match3 = pattern3.search(condition)
    if match3:
        target = match3.group(1).lower()
        direction = match3.group(2).lower()
        value = float(match3.group(3))
        
        parsed.action = "limit"
        parsed.target = _normalize_target(target)
        parsed.value = value
        # Store direction in unit field for now
        parsed.unit = "below" if direction in ["below", "under"] else "above"
        return parsed
    
    # Pattern 4: "Limit X to Y"
    pattern4 = re.compile(r'limit\s+(\w+)\s+to\s+([\d.]+)', re.IGNORECASE)
    match4 = pattern4.search(condition)
    if match4:
        target = match4.group(1).lower()
        value = float(match4.group(2))
        
        parsed.action = "limit"
        parsed.target = _normalize_target(target)
        parsed.value = value
        return parsed
    
    # Pattern 5: Simple "Set X Y" (e.g., "Set complexity 0.3")
    pattern5 = re.compile(r'set\s+(\w+)\s+([\d.]+)', re.IGNORECASE)
    match5 = pattern5.search(condition)
    if match5:
        target = match5.group(1).lower()
        value = float(match5.group(2))
        
        parsed.action = "set"
        parsed.target = _normalize_target(target)
        parsed.value = value
        return parsed
    
    # If no pattern matches, return unknown condition
    logger.warning(f"Could not parse condition: {condition}")
    return parsed

def _normalize_target(target: str) -> str:
    """Normalize target names to standard format"""
    target_lower = target.lower()
    
    # Map common variations to standard names
    mappings = {
        "complexity": "complexity",
        "risk": "risk_score",
        "risk_score": "risk_score",
        "risk_threshold": "risk_threshold",
        "coherence": "coherence",
        "coherence_threshold": "coherence_threshold",
        "monitoring": "monitoring_duration",
        "monitor": "monitoring_duration",
        "duration": "monitoring_duration",
        "time": "monitoring_duration",
        "risk": "risk_score",
        "risk_score": "risk_score",
    }
    
    return mappings.get(target_lower, target_lower)

async def apply_condition(parsed: ParsedCondition, agent_id: str, mcp_server) -> Dict[str, Any]:
    """
    Apply a parsed condition to agent state/metadata.
    
    Args:
        parsed: ParsedCondition object
        agent_id: Agent ID to apply condition to
        mcp_server: MCP server instance (for accessing agent metadata)
    
    Returns:
        Dict with application result
    """
    result = {
        "condition": parsed.original,
        "parsed": parsed.to_dict(),
        "status": "applied",
        "changes": {}
    }
    
    # Wave 2 audit: force=True dropped per PR #350 precedent. Single-agent
    # existence check; in-memory cache is fresh enough.
    await mcp_server.load_metadata_async()
    
    if agent_id not in mcp_server.agent_metadata:
        result["status"] = "failed"
        result["error"] = f"Agent '{agent_id}' not found"
        return result
    
    meta = mcp_server.agent_metadata[agent_id]
    
    # Apply condition based on action and target
    try:
        if parsed.action == "set":
            if parsed.target == "complexity":
                # Persist a complexity cap for subsequent updates (enforced in process_agent_update)
                meta.dialectic_conditions.append({
                    "type": "complexity_limit",
                    "value": parsed.value,
                    "applied_at": datetime.now().isoformat()
                })
                result["changes"]["complexity_limit"] = parsed.value
            elif parsed.target == "risk_score":
                # Can't directly set risk_score (it's computed), but can note it
                meta.dialectic_conditions.append({
                    "type": "risk_target",
                    "value": parsed.value,
                    "applied_at": datetime.now().isoformat()
                })
                result["changes"]["risk_target"] = parsed.value
            elif parsed.target == "coherence":
                # Can't directly set coherence (it's computed), but can note it
                meta.dialectic_conditions.append({
                    "type": "coherence_target",
                    "value": parsed.value,
                    "applied_at": datetime.now().isoformat()
                })
                result["changes"]["coherence_target"] = parsed.value
        
        elif parsed.action == "monitor":
            if parsed.target == "monitoring_duration":
                # Store monitoring duration in metadata
                duration_hours = parsed.value
                if parsed.unit == "minutes":
                    duration_hours = parsed.value / 60.0
                meta.dialectic_conditions.append({
                    "type": "monitoring_duration",
                    "value": duration_hours,
                    "unit": "hours",
                    "applied_at": datetime.now().isoformat()
                })
                result["changes"]["monitoring_duration_hours"] = duration_hours
        
        elif parsed.action == "limit":
            # Store limit condition
            meta.dialectic_conditions.append({
                "type": f"{parsed.target}_limit",
                "value": parsed.value,
                "direction": parsed.unit,  # "below" or "above"
                "applied_at": datetime.now().isoformat()
            })
            result["changes"][f"{parsed.target}_limit"] = parsed.value
        
        elif parsed.action in ["reduce", "increase"]:
            # Store adjustment condition
            meta.dialectic_conditions.append({
                "type": f"{parsed.target}_adjustment",
                "action": parsed.action,
                "target_value": parsed.value,
                "applied_at": datetime.now().isoformat()
            })
            result["changes"][f"{parsed.target}_adjustment"] = {
                "action": parsed.action,
                "target": parsed.value
            }
        
        logger.info(f"Applied condition to agent '{agent_id}': {parsed.original}")

        # Note: Dialectic conditions are stored in runtime cache and persisted
        # via agent_storage when the agent's state is updated
        
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        logger.error(f"Error applying condition to agent '{agent_id}': {e}", exc_info=True)
    
    return result

