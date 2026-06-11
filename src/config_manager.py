"""
Unified Configuration Manager

Provides a single access point for all configuration values across the system.
Documents which configs are runtime-changeable vs static.

Configuration Sources:
1. Static config: config/governance_config.py (GovernanceConfig class)
2. Core parameters: governance_core.parameters (DynamicsParams, Theta) — compiled in unitares-core
3. Runtime overrides: src/runtime_config.py (threshold overrides)
4. Server constants: src/mcp_server_std.py (MAX_KEEP_PROCESSES, etc.)
"""

from typing import Dict, Any, Optional
from dataclasses import dataclass

# Import static configs
from config import governance_config as static_config_module
from governance_core.parameters import DynamicsParams
from src.runtime_config import get_thresholds, get_effective_threshold, set_thresholds as _set_thresholds


@dataclass
class ConfigSource:
    """Metadata about a configuration value"""
    value: Any
    source: str  # "static", "runtime", "core", "server", "env"
    changeable: bool  # Can be changed at runtime
    description: Optional[str] = None


class ConfigManager:
    """
    Unified configuration manager.
    
    Provides access to all configuration values with metadata about their source
    and whether they can be changed at runtime.
    """
    
    def __init__(self):
        self._static_config = static_config_module.GovernanceConfig()
        self._core_params = DynamicsParams()
    
    def get_thresholds(self) -> Dict[str, float]:
        """
        Get all governance thresholds (runtime overrides + defaults).
        
        Returns:
            Dict of threshold_name -> value
        """
        return get_thresholds()
    
    def get_threshold(self, name: str, default: Optional[float] = None) -> float:
        """
        Get a specific threshold value.
        
        Args:
            name: Threshold name (e.g., "risk_approve_threshold")
            default: Optional default if not found
        
        Returns:
            Threshold value
        """
        return get_effective_threshold(name, default)
    
    def set_thresholds(self, thresholds: Dict[str, float], validate: bool = True) -> Dict[str, Any]:
        """
        Set runtime threshold overrides.
        
        Only works for thresholds marked as runtime-changeable.
        
        Args:
            thresholds: Dict of threshold_name -> value
            validate: If True, validate values are in reasonable ranges
        
        Returns:
            {
                "success": bool,
                "updated": List[str],
                "errors": List[str]
            }
        """
        return _set_thresholds(thresholds, validate)
    
    def get_static_config(self) -> Any:
        """
        Get static configuration (GovernanceConfig instance).
        
        This config is NOT runtime-changeable. Modify governance_config.py to change.
        """
        return self._static_config
    
    def get_core_params(self) -> DynamicsParams:
        """
        Get core dynamics parameters.
        
        These are NOT runtime-changeable. Defined in governance_core.parameters (unitares-core).
        """
        return self._core_params
    
    def get_server_constants(self) -> Dict[str, Any]:
        """
        Get server-level constants (MAX_KEEP_PROCESSES, etc.).
        
        These are NOT runtime-changeable. Modify agent_state.py to change.
        """
        # Import here to avoid circular dependencies
        try:
            import src.agent_state as mcp_server
            return {
                "MAX_KEEP_PROCESSES": mcp_server.MAX_KEEP_PROCESSES,
                "SERVER_VERSION": mcp_server.SERVER_VERSION,
                "SERVER_BUILD_DATE": mcp_server.SERVER_BUILD_DATE,
            }
        except (ImportError, AttributeError):
            # Fallback if not available
            return {
                "MAX_KEEP_PROCESSES": 42,
                "SERVER_VERSION": "unknown",
                "SERVER_BUILD_DATE": "unknown",
            }
    
    def get_all_config(self) -> Dict[str, ConfigSource]:
        """
        Get all configuration values with metadata.
        
        Returns:
            Dict mapping config_name -> ConfigSource with value, source, changeable flag
        """
        configs = {}
        
        # Runtime-changeable thresholds
        thresholds = self.get_thresholds()
        for name, value in thresholds.items():
            configs[f"threshold.{name}"] = ConfigSource(
                value=value,
                source="runtime",
                changeable=True,
                description=f"Governance threshold: {name}"
            )
        
        # Static config values (sample - not all)
        static = self._static_config
        configs["static.RISK_APPROVE_THRESHOLD"] = ConfigSource(
            value=static.RISK_APPROVE_THRESHOLD,
            source="static",
            changeable=False,
            description="Static risk approve threshold (default)"
        )
        configs["static.RISK_REVISE_THRESHOLD"] = ConfigSource(
            value=static.RISK_REVISE_THRESHOLD,
            source="static",
            changeable=False,
            description="Static risk revise threshold (default)"
        )
        configs["static.COHERENCE_CRITICAL_THRESHOLD"] = ConfigSource(
            value=static.COHERENCE_CRITICAL_THRESHOLD,
            source="static",
            changeable=False,
            description="Static coherence critical threshold (default)"
        )
        
        # Core parameters
        core = self._core_params
        configs["core.alpha"] = ConfigSource(
            value=core.alpha,
            source="core",
            changeable=False,
            description="E dynamics: I → E coupling strength"
        )
        configs["core.mu"] = ConfigSource(
            value=core.mu,
            source="core",
            changeable=False,
            description="S dynamics: S decay rate"
        )
        
        # Server constants
        server = self.get_server_constants()
        configs["server.MAX_KEEP_PROCESSES"] = ConfigSource(
            value=server["MAX_KEEP_PROCESSES"],
            source="server",
            changeable=False,
            description="Maximum processes to keep before cleanup"
        )
        
        return configs
    
    def get_config_info(self) -> Dict[str, Any]:
        """
        Get configuration metadata and documentation.
        
        Returns:
            Dict with config categories and their changeability
        """
        return {
            "runtime_changeable": {
                "description": "Can be changed at runtime via set_thresholds()",
                "configs": [
                    "threshold.risk_approve_threshold",
                    "threshold.risk_revise_threshold",
                    "threshold.coherence_critical_threshold",
                    "threshold.void_threshold_initial",
                ]
            },
            "static": {
                "description": "Defined in config/governance_config.py - requires code change",
                "configs": [
                    "RISK_APPROVE_THRESHOLD (default)",
                    "RISK_REVISE_THRESHOLD (default)",
                    "COHERENCE_CRITICAL_THRESHOLD (default)",
                    "All GovernanceConfig class attributes",
                ]
            },
            "core": {
                "description": "Defined in governance_core.parameters (unitares-core) - requires rebuild",
                "configs": [
                    "DynamicsParams (alpha, mu, kappa, delta, etc.)",
                    "Theta (C1, eta1)",
                ]
            },
            "server": {
                "description": "Defined in src/mcp_server_std.py - requires code change",
                "configs": [
                    "MAX_KEEP_PROCESSES",
                    "SERVER_VERSION",
                    "HEARTBEAT_CONFIG",
                ]
            }
        }


# Singleton instance
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """Get the global ConfigManager instance"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


# Convenience functions for common access patterns
def get_thresholds() -> Dict[str, float]:
    """Get all thresholds (convenience wrapper)"""
    return get_config_manager().get_thresholds()


def get_threshold(name: str, default: Optional[float] = None) -> float:
    """Get a specific threshold (convenience wrapper)"""
    return get_config_manager().get_threshold(name, default)


def set_thresholds(thresholds: Dict[str, float], validate: bool = True) -> Dict[str, Any]:
    """Set thresholds (convenience wrapper)"""
    return get_config_manager().set_thresholds(thresholds, validate)

