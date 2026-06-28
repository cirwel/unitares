"""Tests for gateway.simplifiers — response envelope transformations."""

from src.gateway.simplifiers import (
    ok, err,
    simplify_status, simplify_checkin, simplify_search, simplify_note, simplify_query,
)


class TestEnvelopes:
    def test_ok(self):
        result = ok("All good", {"key": "val"})
        assert result == {"ok": True, "summary": "All good", "data": {"key": "val"}}

    def test_ok_no_data(self):
        result = ok("Done")
        assert result == {"ok": True, "summary": "Done", "data": {}}

    def test_err(self):
        result = err("Failed", "details here")
        assert result == {"ok": False, "summary": "Failed", "error": "details here"}

    def test_err_no_error_detail(self):
        result = err("Failed")
        assert result == {"ok": False, "summary": "Failed", "error": "Failed"}


class TestSimplifyStatus:
    def test_full_response(self):
        raw = {
            "eisv": {"E": 0.8, "I": 0.7, "S": 0.3, "V": 0.1},
            "coherence": 0.49856,
            "basin": "high",
            "risk": 0.35,
            "action": "proceed",
            "resolved_agent_id": "abc-123",
        }
        result = simplify_status(raw)
        assert result["ok"] is True
        assert "proceed" in result["summary"]
        assert result["data"]["eisv"] == {"E": 0.8, "I": 0.7, "S": 0.3, "V": 0.1}
        assert result["data"]["coherence"] == 0.4986
        assert result["data"]["basin"] == "high"

    def test_nested_state(self):
        raw = {
            "state": {
                "eisv": {"energy": 0.8, "information_integrity": 0.7, "entropy": 0.3, "void": 0.1},
                "coherence": {"value": 0.5},
                "basin": "high",
            }
        }
        result = simplify_status(raw)
        assert result["ok"] is True
        assert result["data"]["eisv"]["E"] == 0.8

    def test_non_dict(self):
        result = simplify_status("some string")
        assert result["ok"] is True


class TestSimplifyCheckin:
    def test_proceed(self):
        raw = {"action": "proceed", "margin": "comfortable", "reason": "Good work", "coherence": 0.5}
        result = simplify_checkin(raw)
        assert result["ok"] is True
        assert result["data"]["verdict"] == "proceed"
        assert result["data"]["margin"] == "comfortable"
        assert "proceed" in result["summary"]

    def test_guide(self):
        raw = {"action": "guide", "margin": "tight", "guidance": "Slow down"}
        result = simplify_checkin(raw)
        assert result["data"]["verdict"] == "guide"
        assert result["data"]["guidance"] == "Slow down"


class TestSimplifySearch:
    def test_with_results(self):
        raw = {
            "results": [
                {"title": "Discovery 1", "content": "Found pattern", "tags": ["redis"], "score": 0.95},
                {"title": "Discovery 2", "content": "Another find", "severity": "low"},
            ]
        }
        result = simplify_search(raw)
        assert result["ok"] is True
        assert "2 result(s)" in result["summary"]
        assert len(result["data"]["results"]) == 2
        assert result["data"]["results"][0]["title"] == "Discovery 1"

    def test_empty_results(self):
        raw = {"results": []}
        result = simplify_search(raw)
        assert "0 result(s)" in result["summary"]

    def test_entries_key(self):
        raw = {"entries": [{"content": "test"}]}
        result = simplify_search(raw)
        assert len(result["data"]["results"]) == 1


class TestSimplifyNote:
    def test_with_id(self):
        raw = {"node_id": "n-123", "status": "created"}
        result = simplify_note(raw)
        assert result["ok"] is True
        assert result["data"]["id"] == "n-123"
        assert result["data"]["saved"] is True

    def test_with_canonical_note_id(self):
        raw = {"note_id": "n-456", "message": "Note saved"}
        result = simplify_note(raw)
        assert result["ok"] is True
        assert result["data"]["id"] == "n-456"

    def test_without_id(self):
        raw = {"status": "ok"}
        result = simplify_note(raw)
        assert result["data"]["saved"] is True


class TestSimplifyQuery:
    def test_with_response(self):
        raw = {"response": "Your coherence is 0.5"}
        result = simplify_query(raw)
        assert result["ok"] is True
        assert "coherence" in result["summary"]

    def test_non_dict(self):
        result = simplify_query("plain text")
        assert result["ok"] is True
