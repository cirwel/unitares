"""save_floor_governed — route the watcher floor through governed file_write,
with a guaranteed atomic-local fallback so the floor is never lost (fail-open)."""

from __future__ import annotations

from agents.watcher.floor_state import FloorState, load_floor, save_floor_governed

_PROPOSE = "unitares_sdk.lease_plane.client.LeasePlaneClient.propose_file_write"


def _state() -> FloorState:
    return FloorState(updated_at="2026-01-01T00:00:00Z", buckets={})


def test_governed_path_commits_and_does_not_write_locally(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_propose(self, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "effect_id": "e1"}

    monkeypatch.setattr(_PROPOSE, fake_propose)
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")

    ok = save_floor_governed(
        _state(),
        proposer_uuid="u",
        continuity_token="c",
        session_id="s",
        state_dir=tmp_path,
    )

    assert ok is True
    assert captured["proposer_uuid"] == "u"
    assert captured["path"].endswith("pattern_floor.json")
    # the plane committed it — no local atomic fallback fired
    assert not (tmp_path / "pattern_floor.json").exists()


def test_governed_rejection_falls_back_to_atomic_write(monkeypatch, tmp_path):
    monkeypatch.setattr(_PROPOSE, lambda self, **k: {"ok": False, "error": "governance_blocked"})
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")

    ok = save_floor_governed(
        _state(), proposer_uuid="u", continuity_token="c", session_id="s", state_dir=tmp_path
    )

    assert ok is False
    # the floor is NEVER lost — the atomic local write happened
    assert (tmp_path / "pattern_floor.json").exists()
    assert load_floor(state_dir=tmp_path).updated_at == "2026-01-01T00:00:00Z"


def test_governed_exception_falls_back(monkeypatch, tmp_path):
    def boom(self, **k):
        raise RuntimeError("plane down")

    monkeypatch.setattr(_PROPOSE, boom)
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")

    ok = save_floor_governed(
        _state(), proposer_uuid="u", continuity_token="c", session_id="s", state_dir=tmp_path
    )
    assert ok is False
    assert (tmp_path / "pattern_floor.json").exists()


def test_missing_bearer_falls_back_without_calling_the_plane(monkeypatch, tmp_path):
    called = {"n": 0}

    def fake_propose(self, **k):
        called["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(_PROPOSE, fake_propose)
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)

    ok = save_floor_governed(
        _state(), proposer_uuid="u", continuity_token="c", session_id="s", state_dir=tmp_path
    )
    assert ok is False
    assert called["n"] == 0  # no bearer -> never even attempts the plane
    assert (tmp_path / "pattern_floor.json").exists()
