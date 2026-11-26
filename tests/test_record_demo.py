from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _load_record_demo():
    path = Path(__file__).resolve().parents[1] / "scripts" / "demo" / "record_demo.py"
    spec = importlib.util.spec_from_file_location("record_demo", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


record_demo = _load_record_demo()


def test_governance_port_precedence(tmp_path: Path):
    dotenv = tmp_path / ".env"
    dotenv.write_text('GOVERNANCE_HOST_PORT="19767"\n', encoding="utf-8")

    assert record_demo._governance_port({}, dotenv) == "19767"
    assert record_demo._governance_port({"GOVERNANCE_HOST_PORT": "18767"}, dotenv) == "18767"
    assert (
        record_demo._governance_port(
            {"UNITARES_DEMO_PORT": "17767", "GOVERNANCE_HOST_PORT": "18767"},
            dotenv,
        )
        == "17767"
    )
    assert record_demo._governance_port({}, tmp_path / "missing.env") == "8767"


def test_auth_headers_refuse_non_loopback_http():
    assert record_demo._auth_headers("http://127.0.0.1:8767/v1/tools/call", {"UNITARES_HTTP_API_TOKEN": "tok"}) == {
        "Authorization": "Bearer tok"
    }
    assert record_demo._auth_headers("https://gov.example.com/v1/tools/call", {"UNITARES_HTTP_API_TOKEN": "tok"}) == {
        "Authorization": "Bearer tok"
    }
    with pytest.raises(ValueError, match="non-loopback"):
        record_demo._auth_headers("http://gov.example.com/v1/tools/call", {"UNITARES_HTTP_API_TOKEN": "tok"})


class _FakeRecordServer:
    def __init__(self, *, age_enabled: bool = True) -> None:
        self.age_enabled = age_enabled
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.discoveries: dict[str, dict[str, Any]] = {}
        self.released: set[str] = set()
        self._agent_seq = 0
        self._disc_seq = 0

    def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        if tool == "start_session":
            self._agent_seq += 1
            uid = f"00000000-0000-0000-0000-00000000000{self._agent_seq}"
            sid = f"agent-{uid[:8]}-sess"
            res: dict[str, Any] = {
                "agent_uuid": uid,
                "client_session_id": sid,
                "display_name": args.get("name"),
                "is_new": True,
            }
            if args.get("parent_agent_id"):
                assert args["parent_agent_id"] in self.released, "predecessor must have released presence"
                res["state_summary"] = {
                    "lineage_state": "provisional",
                    "episode_fork_kind": "identity_lineage",
                    "predecessor_uuid": args["parent_agent_id"],
                }
            return res

        if tool == "store_finding":
            self._disc_seq += 1
            did = f"2026-11-25T16:00:0{self._disc_seq}+00:00"
            self.discoveries[did] = {
                "discovery_id": did,
                "summary": args["summary"],
                "status": "open",
                "tags": list(args.get("tags") or []),
                "type": args.get("discovery_type"),
            }
            return {"discovery_id": did, "state_summary": {"status": "open"}}

        if tool == "knowledge" and args.get("action") == "supersede":
            if not self.age_enabled:
                return {"success": False, "error": "SUPERSEDES edges require AGE graph backend"}
            old = self.discoveries[args["supersedes_id"]]
            old["status"] = "superseded"
            old["superseded_by"] = args["discovery_id"]
            return {"success": True, "edge_created": True}

        if tool == "update_finding":
            row = self.discoveries[args["discovery_id"]]
            row["status"] = args["status"]
            if args.get("superseded_by"):
                row["superseded_by"] = args["superseded_by"]
            return {"success": True, "status": args["status"]}

        if tool == "sync_state":
            return {
                "prediction_id": "pred-demo-1234",
                "action_summary": {"action": "proceed", "verdict_confidence": "provisional"},
            }

        if tool == "record_result":
            assert args["detail"]["do_not_use_for_live_validation"] is True
            return {
                "state_summary": {
                    "outcome_type": args["outcome_type"],
                    "outcome_score": args["outcome_score"],
                    "prediction_binding": "registry" if args.get("prediction_id") else "none",
                    "corroboration_grade": "self_report_with_refs",
                }
            }

        if tool == "search_shared_memory":
            # Verify Agent C searches without passing any session identity.
            assert "client_session_id" not in args
            wanted = set(args.get("tags") or [])
            matches = [
                dict(row)
                for row in self.discoveries.values()
                if not wanted or wanted.intersection(row["tags"])
            ]
            return {"success": True, "memory_suggestions": matches}

        if tool == "agent" and args.get("action") == "release_presence":
            sid = args["client_session_id"]
            # Map agent-<8>-sess back to full fake uuid
            prefix = sid.split("-")[1]
            for i in range(1, self._agent_seq + 1):
                uid = f"00000000-0000-0000-0000-00000000000{i}"
                if uid.startswith(prefix):
                    self.released.add(uid)
            return {"action": "release_presence", "released": True}

        raise AssertionError(f"Unexpected tool call: {tool} {args}")


def test_run_record_demo_with_age_supersede():
    server = _FakeRecordServer(age_enabled=True)
    summary = record_demo.run_record_demo(server, run_id="abcd1234", dashboard_url="http://localhost:8767/dashboard")

    assert summary["agent_b"]["supersede_mode"] == "age_supersedes_edge"
    assert summary["agent_b"]["supersede_ok"] is True
    assert summary["checks"] == {
        "agent_a_released": True,
        "agent_b_released": True,
        "agent_c_released": True,
        "search_found_initial_claim": True,
        "search_found_replacement_claim": True,
        "initial_claim_marked_superseded": True,
        "initial_claim_links_replacement": True,
        "outcome_bound_to_prediction": True,
        "lineage_points_to_agent_a": True,
    }
    text = record_demo.format_report(summary)
    assert "run abcd1234" in text
    assert "age_supersedes_edge" in text
    assert "http://localhost:8767/dashboard" in text


def test_run_record_demo_falls_back_when_age_unavailable():
    server = _FakeRecordServer(age_enabled=False)
    summary = record_demo.run_record_demo(server, run_id="beef5678")

    assert summary["agent_b"]["supersede_mode"] == "relational_status_fallback"
    assert summary["checks"]["initial_claim_marked_superseded"] is True
    assert summary["checks"]["initial_claim_links_replacement"] is True
    assert any(tool == "update_finding" for tool, _ in server.calls)


def test_demo_tags_use_ephemeral_non_permanent_types():
    server = _FakeRecordServer(age_enabled=True)
    record_demo.run_record_demo(server, run_id="tagcheck")
    stores = [args for tool, args in server.calls if tool == "store_finding"]
    assert len(stores) == 2
    for args in stores:
        assert "demo" in args["tags"]
        assert "ephemeral" in args["tags"]
        assert "permanent" not in args["tags"]
        assert args["discovery_type"] not in {"architectural_decision", "learning", "pattern", "rule"}


def test_cli_json_output_via_monkeypatch(monkeypatch, capsys):
    server = _FakeRecordServer(age_enabled=True)
    monkeypatch.setattr(record_demo, "preflight", lambda _url: None)
    monkeypatch.setattr(record_demo, "_http_call", lambda _url, tool, args, timeout=15.0: server(tool, args))

    rc = record_demo.main(["--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["lineage_points_to_agent_a"] is True
