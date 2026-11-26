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
    _AGENT_UUIDS = (
        "a1111111-0000-0000-0000-000000000001",
        "b2222222-0000-0000-0000-000000000002",
        "c3333333-0000-0000-0000-000000000003",
    )

    def __init__(self, *, age_enabled: bool = True, fail_sync_state: bool = False) -> None:
        self.age_enabled = age_enabled
        self.fail_sync_state = fail_sync_state
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.discoveries: dict[str, dict[str, Any]] = {}
        self.session_to_uuid: dict[str, str] = {}
        self.released: set[str] = set()
        self._agent_seq = 0
        self._disc_seq = 0

    def _require_session_uuid(self, args: dict[str, Any]) -> str:
        sid = args.get("client_session_id")
        assert isinstance(sid, str) and sid in self.session_to_uuid, f"unknown client_session_id: {sid!r}"
        return self.session_to_uuid[sid]

    def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        if tool == "start_session":
            uid = self._AGENT_UUIDS[self._agent_seq]
            self._agent_seq += 1
            sid = f"agent-{uid[:8]}-sess"
            self.session_to_uuid[sid] = uid
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
            writer = self._require_session_uuid(args)
            self._disc_seq += 1
            did = f"2026-11-25T16:00:0{self._disc_seq}+00:00"
            self.discoveries[did] = {
                "discovery_id": did,
                "agent_id": writer,
                "summary": args["summary"],
                "status": "open",
                "tags": list(args.get("tags") or []),
                "type": args.get("discovery_type"),
            }
            return {"discovery_id": did, "state_summary": {"status": "open"}}

        if tool == "knowledge" and args.get("action") == "supersede":
            writer = self._require_session_uuid(args)
            assert writer == self._AGENT_UUIDS[1], "only Agent B should supersede A's finding"
            if not self.age_enabled:
                return {"success": False, "error": "SUPERSEDES edges require AGE graph backend"}
            old = self.discoveries[args["supersedes_id"]]
            old["status"] = "superseded"
            old["superseded_by"] = args["discovery_id"]
            return {"success": True, "edge_created": True}

        if tool == "update_finding":
            writer = self._require_session_uuid(args)
            assert writer == self._AGENT_UUIDS[1], "only Agent B should run fallback update"
            row = self.discoveries[args["discovery_id"]]
            row["status"] = args["status"]
            # Real server relational fallback flips status only; superseded_by comes
            # exclusively from the AGE edge (mutation_helpers._record_supersession_edge).
            if self.age_enabled and args.get("superseded_by"):
                row["superseded_by"] = args["superseded_by"]
                return {"success": True, "status": args["status"]}
            return {
                "success": True,
                "status": args["status"],
                "supersession_warning": "SUPERSEDES edges require AGE graph backend",
            }

        if tool == "sync_state":
            writer = self._require_session_uuid(args)
            assert writer == self._AGENT_UUIDS[1], "only Agent B should check in"
            if self.fail_sync_state:
                return {"success": False, "error": "AGENT_PAUSED"}
            return {
                "prediction_id": "pred-demo-1234",
                "action_summary": {"action": "proceed", "verdict_confidence": "provisional"},
            }

        if tool == "record_result":
            writer = self._require_session_uuid(args)
            assert writer == self._AGENT_UUIDS[1], "only Agent B should record outcome"
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
            uid = self._require_session_uuid(args)
            self.released.add(uid)
            return {"action": "release_presence", "released": True}

        raise AssertionError(f"Unexpected tool call: {tool} {args}")


def test_run_record_demo_with_age_supersede():
    server = _FakeRecordServer(age_enabled=True)
    summary = record_demo.run_record_demo(server, run_id="abcd1234", dashboard_url="http://localhost:8767/dashboard")

    assert summary["agent_b"]["supersede_mode"] == "age_supersedes_edge"
    assert summary["agent_b"]["supersede_ok"] is True
    assert record_demo._all_required_checks_passed(summary) is True
    assert summary["checks"] == {
        "agent_a_released": True,
        "agent_b_released": True,
        "agent_c_released": True,
        "supersede_succeeded": True,
        "search_found_initial_claim": True,
        "search_found_replacement_claim": True,
        "initial_claim_marked_superseded": True,
        "initial_claim_links_replacement": True,
        "outcome_bound_to_prediction": True,
        "lineage_points_to_agent_a": True,
    }
    text = record_demo.format_report(summary)
    assert "run abcd1234" in text
    assert "age_supersedes_edge (ok)" in text
    assert "http://localhost:8767/dashboard" in text


def test_run_record_demo_falls_back_honestly_when_age_unavailable():
    server = _FakeRecordServer(age_enabled=False)
    summary = record_demo.run_record_demo(server, run_id="beef5678")

    assert summary["agent_b"]["supersede_mode"] == "relational_status_fallback"
    assert summary["agent_b"]["supersede_warning"] is not None
    assert summary["checks"]["initial_claim_marked_superseded"] is True
    assert summary["checks"]["initial_claim_links_replacement"] is False
    assert record_demo._all_required_checks_passed(summary) is True
    assert any(tool == "update_finding" for tool, _ in server.calls)
    text = record_demo.format_report(summary)
    assert "status flipped; no AGE graph edge" in text
    assert "[SKIP] initial_claim_links_replacement" in text


def test_demo_fails_nonzero_when_sync_state_errors_and_skips_unbound_outcome(monkeypatch, capsys):
    server = _FakeRecordServer(age_enabled=True, fail_sync_state=True)
    monkeypatch.setattr(record_demo, "preflight", lambda _url: None)
    monkeypatch.setattr(
        record_demo,
        "_http_call",
        lambda _url, tool, args, timeout=15.0, user_agent=None: server(tool, args),
    )

    rc = record_demo.main(["--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["outcome_bound_to_prediction"] is False
    assert not any(tool == "record_result" for tool, _ in server.calls)


def test_mid_run_error_releases_active_sessions_and_exits_cleanly(monkeypatch, capsys):
    released: list[str] = []

    def flaky_server(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "start_session":
            return {"agent_uuid": "a1111111-0000-0000-0000-000000000001", "client_session_id": "agent-a1111111-sess"}
        if tool == "store_finding":
            raise record_demo.DemoCallError("store_finding returned HTTP 401")
        if tool == "agent" and args.get("action") == "release_presence":
            released.append(args["client_session_id"])
            return {"released": True}
        raise AssertionError(f"unexpected {tool}")

    monkeypatch.setattr(record_demo, "preflight", lambda _url: None)
    monkeypatch.setattr(
        record_demo,
        "_http_call",
        lambda _url, tool, args, timeout=15.0, user_agent=None: flaky_server(tool, args),
    )

    rc = record_demo.main([])
    assert rc == 1
    assert released == ["agent-a1111111-sess"]
    assert "HTTP 401" in capsys.readouterr().err


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
    seen_uas: dict[str, str | None] = {}

    def fake_http(_url, tool, args, timeout=15.0, user_agent=None):
        seen_uas[tool] = user_agent
        return server(tool, args)

    monkeypatch.setattr(record_demo, "preflight", lambda _url: None)
    monkeypatch.setattr(record_demo, "_http_call", fake_http)

    rc = record_demo.main(["--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["lineage_points_to_agent_a"] is True
    assert seen_uas["search_shared_memory"] is not None
    assert seen_uas["search_shared_memory"].startswith("unitares-record-demo-reader/")
