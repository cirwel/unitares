"""Installation checks work without a personal workspace or desktop client."""

import json
from pathlib import Path

import pytest

from src import workspace_health


@pytest.fixture
def installed_environment(monkeypatch):
    original_import = workspace_health.importlib.import_module

    def import_module(name):
        if name in {"mcp", "numpy"}:
            return object()
        return original_import(name)

    monkeypatch.setattr(workspace_health.importlib, "import_module", import_module)
    monkeypatch.setattr(workspace_health.importlib.util, "find_spec", lambda name: object())


@pytest.mark.parametrize("layout", ["container/app", "python/site-packages"])
def test_headless_install_does_not_require_checkout_or_client_configs(
    installed_environment, monkeypatch, tmp_path, layout
):
    root = tmp_path / layout
    root.mkdir(parents=True)
    monkeypatch.setattr(workspace_health, "get_project_root", lambda: root)

    def unexpected_home_lookup():
        pytest.fail("default diagnostics must not discover personal client configs")

    monkeypatch.setattr(Path, "home", unexpected_home_lookup)
    result = workspace_health.get_workspace_health()

    assert result["health"] == "healthy"
    assert result["mcp_status"]["status"] == "not_requested"
    assert result["mcp_status"]["clients"] == {}
    assert result["documentation_coherence"]["status"] == "not_requested"
    assert result["documentation_coherence"]["total_issues"] == 0
    assert result["workspace_status"]["server_module_available"] is True
    assert "health_check" in result["recommendation"]


def test_default_diagnostics_do_not_read_files(installed_environment, monkeypatch):
    def unexpected_read(*args, **kwargs):
        pytest.fail("no workspace or client files were requested")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert workspace_health.get_workspace_health()["health"] == "healthy"


def test_explicit_client_configs_accept_arbitrary_labels_and_deduplicate_names(tmp_path):
    first = tmp_path / "editor.json"
    first.write_text(json.dumps({"mcpServers": {"governance": {}, "search": {}}}))
    second = tmp_path / "batch.json"
    second.write_text(json.dumps({"mcpServers": {"governance": {}}}))

    result = workspace_health.check_mcp_status({"editor": first, "batch-runner": second})

    assert result["clients"] == {
        "editor": {"status": "read", "servers": ["governance", "search"]},
        "batch-runner": {"status": "read", "servers": ["governance"]},
    }
    assert result["configured_count"] == result["active_count"] == 2
    assert result["cursor_servers"] == result["claude_desktop_servers"] == []


def test_legacy_client_keys_only_mirror_explicit_configurations(tmp_path):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"mcpServers": {"governance": {}}}))

    result = workspace_health.check_mcp_status({"cursor": path, "claude_desktop": path})

    assert result["cursor_servers"] == result["claude_desktop_servers"] == ["governance"]
    assert result["configured_count"] == 1


def test_absent_client_config_is_only_an_issue_when_requested(installed_environment, tmp_path):
    path = tmp_path / "absent-client.json"

    assert workspace_health.get_workspace_health()["health"] == "healthy"
    result = workspace_health.get_workspace_health(client_config_paths={"batch-runner": path})

    assert result["health"] == "moderate"
    assert result["mcp_status"]["clients"]["batch-runner"] == {
        "status": "missing", "servers": []
    }


@pytest.mark.parametrize("content", ["[]", '{"mcpServers": []}', "{"])
def test_invalid_requested_client_config_is_reported(installed_environment, tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content)

    result = workspace_health.get_workspace_health(client_config_paths={"batch-runner": path})

    assert result["health"] == "moderate"
    assert result["mcp_status"]["clients"]["batch-runner"]["status"] == "error"


def test_only_explicit_workspace_files_affect_health(installed_environment, tmp_path):
    (tmp_path / "runbook.txt").write_text("Deployment-specific instructions")

    result = workspace_health.get_workspace_health(
        required_files=["runbook.txt"], workspace_root=tmp_path
    )
    assert result["health"] == "healthy"
    assert result["documentation_coherence"]["file_references_valid"] is True

    result = workspace_health.get_workspace_health(
        required_files=["runbook.txt", "deployment.json"], workspace_root=tmp_path
    )
    assert result["health"] == "moderate"
    assert result["documentation_coherence"]["details"][0]["files"] == ["deployment.json"]


def test_missing_runtime_dependency_is_still_reported(installed_environment, monkeypatch):
    original_import = workspace_health.importlib.import_module

    def import_module(name):
        if name == "mcp":
            raise ModuleNotFoundError("mcp is unavailable")
        return original_import(name)

    monkeypatch.setattr(workspace_health.importlib, "import_module", import_module)
    result = workspace_health.get_workspace_health()

    assert result["health"] == "moderate"
    assert result["workspace_status"]["missing_dependencies"] == ["mcp"]
    assert result["workspace_status"]["dependencies_installed"] is False


def test_missing_server_module_is_still_reported(installed_environment, monkeypatch):
    monkeypatch.setattr(workspace_health.importlib.util, "find_spec", lambda name: None)

    result = workspace_health.get_workspace_health()

    assert result["health"] == "moderate"
    assert result["workspace_status"]["server_module_available"] is False


def test_unperformed_checks_are_unknown_not_successful(installed_environment):
    result = workspace_health.get_workspace_health()

    assert result["security"]["status"] == "not_assessed"
    assert result["security"]["exposed_secrets"] is None
    assert result["security"]["api_keys_secured"] is None
    assert result["workspace_status"]["mcp_servers_responding"] is None
    assert result["workspace_status"]["scripts_executable"] is None
    assert result["documentation_coherence"]["server_counts_match"] is None
    assert result["documentation_coherence"]["paths_current"] is None
