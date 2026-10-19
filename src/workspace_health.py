"""Installation diagnostics with optional, explicitly supplied workspace checks.

The default MCP call checks this server's installed Python environment. Client
configuration and workspace files belong to a deployment, so the Python helpers
inspect them only when a caller supplies their paths. No client home directories
or development-checkout layout are assumed.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any


def get_project_root() -> Path:
    """Return the source installation root for explicit checkout diagnostics."""
    return Path(__file__).parent.parent


def check_mcp_status(
    config_paths: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Read supplied JSON MCP client configurations; never discover clients.

    Keys are arbitrary deployment-defined client labels. Each file uses the
    ``mcpServers`` object. Configured server names are not liveness evidence.
    """
    clients = {}
    configured_servers: set[str] = set()
    for name, raw_path in (config_paths or {}).items():
        path = Path(raw_path).expanduser()
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("MCP client configuration must be a JSON object")
            servers = config.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("mcpServers must be a JSON object")
        except FileNotFoundError:
            clients[name] = {"status": "missing", "servers": []}
        except (OSError, ValueError) as exc:
            clients[name] = {"status": "error", "servers": [], "error": str(exc)}
        else:
            clients[name] = {"status": "read", "servers": sorted(servers)}
            configured_servers.update(servers)

    return {
        "status": "checked" if config_paths else "not_requested",
        "clients": clients,
        "configured_count": len(configured_servers),
        # Compatibility aliases for clients of the original diagnostic. These
        # are populated only by explicitly supplied configurations, like any
        # other client, and do not trigger vendor-specific path discovery.
        "cursor_servers": clients.get("cursor", {}).get("servers", []),
        "claude_desktop_servers": clients.get("claude_desktop", {}).get("servers", []),
        "active_count": len(configured_servers),
        "notes": (
            "Only explicitly supplied client configurations are inspected. "
            "configured_count and legacy active_count count distinct configured "
            "server names, not running servers."
        ),
    }


def check_documentation_coherence(
    required_files: Iterable[str | Path] | None = None,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Check files an embedding caller explicitly requires for its workspace.

    A packaged/headless install has no required checkout documents by default.
    This checks file presence only, not document contents or server counts.
    """
    expected = list(required_files or ())
    missing = []
    if expected:
        root = Path(workspace_root) if workspace_root is not None else get_project_root()
        missing = [str(path) for path in expected if not (root / path).is_file()]
    details = ([{
        "type": "missing_workspace_files",
        "files": missing,
        "severity": "low",
    }] if missing else [])
    return {
        "status": "checked" if expected else "not_requested",
        "server_counts_match": None,
        "file_references_valid": not missing if expected else None,
        "paths_current": None,
        "total_issues": len(details),
        "details": details,
    }


def check_security() -> dict[str, Any]:
    """Retain the response block without claiming an unperformed security scan."""
    return {
        "status": "not_assessed",
        "exposed_secrets": None,
        "api_keys_secured": None,
        "notes": "This installation diagnostic does not inspect secrets or assess their storage.",
    }


def check_workspace_status() -> dict[str, Any]:
    """Check installed dependencies and discoverability of the server module.

    The server is not imported or started, and no network or runtime-health
    probe is performed. Use ``health_check`` for cached subsystem health.
    """
    missing_dependencies = []
    for dependency in ("mcp", "numpy"):
        try:
            importlib.import_module(dependency)
        except ImportError:
            missing_dependencies.append(dependency)
    try:
        server_module_available = importlib.util.find_spec("src.mcp_server_std") is not None
    except (ImportError, ValueError):
        server_module_available = False
    return {
        "status": "checked",
        "dependencies_installed": not missing_dependencies,
        "missing_dependencies": missing_dependencies,
        "server_module_available": server_module_available,
        # Legacy keys remain present, but unknown checks must not read as a
        # successful executable-permission test or live server ping.
        "scripts_executable": None,
        "mcp_servers_responding": None,
        "notes": "Import availability only; no live server or executable-permission probe.",
    }


def get_workspace_health(
    *,
    client_config_paths: Mapping[str, str | Path] | None = None,
    required_files: Iterable[str | Path] | None = None,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Report installation health and any explicitly requested local checks.

    The MCP handler calls this without arguments. Optional filesystem checks
    are Python embedding hooks, not remote caller-controlled file access.
    """
    try:
        mcp_status = check_mcp_status(client_config_paths)
        doc_coherence = check_documentation_coherence(
            required_files, workspace_root=workspace_root
        )
        security = check_security()
        workspace_status = check_workspace_status()
        total_issues = (
            doc_coherence["total_issues"]
            + sum(client["status"] != "read" for client in mcp_status["clients"].values())
            + (0 if workspace_status["dependencies_installed"] else 1)
            + (0 if workspace_status["server_module_available"] else 1)
        )
        if total_issues == 0:
            health = "healthy"
            recommendation = (
                "Installation checks passed. Use health_check for running server subsystem health."
            )
        elif total_issues <= 2:
            health = "moderate"
            recommendation = "Review the reported installation or explicitly requested workspace issues."
        else:
            health = "unhealthy"
            recommendation = "Multiple installation or requested workspace checks need attention."
        return {
            "mcp_status": mcp_status,
            "documentation_coherence": doc_coherence,
            "security": security,
            "workspace_status": workspace_status,
            "last_validated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "health": health,
            "recommendation": recommendation,
        }
    except Exception as exc:
        return {
            "mcp_status": {"error": str(exc)},
            "documentation_coherence": {"error": str(exc)},
            "security": {"error": str(exc)},
            "workspace_status": {"error": str(exc)},
            "last_validated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "health": "error",
            "recommendation": f"Health check encountered an error: {exc}",
        }
