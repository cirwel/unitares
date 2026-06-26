"""Tests for scripts/diagnostics/mcp_call.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def mcp_call_module():
    project_root = Path(__file__).resolve().parent.parent
    module_path = project_root / "scripts" / "diagnostics" / "mcp_call.py"
    spec = importlib.util.spec_from_file_location("diagnostics_mcp_call", module_path)
    assert spec and spec.loader, f"could not load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["diagnostics_mcp_call"] = module
    spec.loader.exec_module(module)
    return module


def test_resolve_session_binding_promotes_argument_to_header(mcp_call_module, tmp_path):
    arguments = {"client_session_id": "agent-existing"}

    session_id, cached = mcp_call_module.resolve_session_binding(
        "process_agent_update",
        arguments,
        None,
        workspace=tmp_path,
        use_cache=True,
    )

    assert session_id == "agent-existing"
    assert cached is None
    assert arguments == {"client_session_id": "agent-existing"}


def test_resolve_session_binding_uses_newest_cache_entry(mcp_call_module, tmp_path):
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-agent-old.json").write_text(
        '{"schema_version": 2, "uuid": "old", '
        '"client_session_id": "agent-old", '
        '"updated_at": "2026-01-01T00:00:00+00:00"}\n'
    )
    (cache_dir / "session-agent-new.json").write_text(
        '{"schema_version": 2, "uuid": "new", '
        '"client_session_id": "agent-new", '
        '"updated_at": "2026-01-02T00:00:00+00:00"}\n'
    )
    arguments = {}

    session_id, cached = mcp_call_module.resolve_session_binding(
        "process_agent_update",
        arguments,
        None,
        workspace=tmp_path,
        use_cache=True,
    )

    assert session_id == "agent-new"
    assert cached["uuid"] == "new"
    assert arguments["client_session_id"] == "agent-new"


def test_retry_with_lineage_mints_and_persists_fresh_session(
    mcp_call_module, tmp_path, monkeypatch
):
    calls: list[tuple[str, dict, str | None]] = []

    def fake_call_tool(base_url, tool_name, arguments, session_id=None):
        calls.append((tool_name, dict(arguments), session_id))
        if tool_name == "onboard":
            return {
                "result": {
                    "success": True,
                    "uuid": "child-uuid",
                    "client_session_id": "agent-child",
                }
            }
        return {"result": {"success": True, "status": "healthy"}}

    monkeypatch.setattr(mcp_call_module, "call_tool", fake_call_tool)
    result = mcp_call_module.maybe_retry_with_lineage(
        base_url="http://example",
        tool_name="process_agent_update",
        arguments={"response_text": "work"},
        workspace=tmp_path,
        cached_entry={"uuid": "parent-uuid", "client_session_id": "agent-parent"},
        result={"result": {"status": "identity_required"}},
        enabled=True,
    )

    assert calls == [
        (
            "onboard",
            {
                "force_new": True,
                "parent_agent_id": "parent-uuid",
                "spawn_reason": "new_session",
                "response_mode": "minimal",
            },
            None,
        ),
        (
            "process_agent_update",
            {"response_text": "work", "client_session_id": "agent-child"},
            "agent-child",
        ),
    ]
    assert result["_mcp_call_auto_lineage"]["parent_agent_id"] == "parent-uuid"
    cache_payload = (tmp_path / ".unitares" / "session-agent-child.json").read_text()
    assert '"uuid": "child-uuid"' in cache_payload
    assert '"continuity_token"' not in cache_payload
