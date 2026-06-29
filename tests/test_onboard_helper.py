"""Tests for the client-side onboard helper.

Covers the behavior described in `scripts/client/onboard_helper.py`:

* successful onboard writes a fresh cache and returns ok
* startup always sends ``force_new=true`` and declares cached UUID lineage
* a failure (including a failed retry) leaves the existing cache untouched
* missing ``uuid`` in the response counts as a failure, not success
* response unwrapping handles both the native MCP envelope and the REST-direct
  shape
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from scripts.client.onboard_helper import (
    _post_json,
    is_successful_onboard,
    run_onboard,
    trajectory_required,
    unwrap_tool_response,
)


class FakePoster:
    """Callable that records each call and returns a queued response."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict, timeout: float, token: str | None) -> dict:
        self.calls.append((url, payload))
        if not self._responses:
            return {}
        return self._responses.pop(0)


def _success_response(
    *, uuid: str = "uuid-ok", agent_id: str = "Claude_Code_X",
    session_id: str = "agent-ok", display_name: str = "acme"
) -> dict:
    return {
        "name": "onboard",
        "result": {
            "success": True,
            "uuid": uuid,
            "agent_id": agent_id,
            "display_name": display_name,
            "client_session_id": session_id,
            "continuity_token": "v1.token-ok",
            "session_resolution_source": "explicit_client_session_id_scoped",
            "continuity_token_supported": True,
        },
    }


def _trajectory_required_response() -> dict:
    return {
        "name": "onboard",
        "result": {
            "success": False,
            "error": "Identity 'acme' has an established trajectory.",
            "recovery": {"reason": "trajectory_required"},
            "agent_signature": {"uuid": None},
        },
    }


# --- unwrap_tool_response --------------------------------------------------

class TestUnwrapToolResponse:
    def test_rest_direct_shape(self) -> None:
        raw = {"result": {"uuid": "abc", "success": True}}
        assert unwrap_tool_response(raw) == {"uuid": "abc", "success": True}

    def test_native_mcp_envelope(self) -> None:
        raw = {
            "result": {
                "content": [{"type": "text", "text": json.dumps({"uuid": "abc"})}]
            }
        }
        assert unwrap_tool_response(raw) == {"uuid": "abc"}

    def test_missing_result_uses_top_level(self) -> None:
        assert unwrap_tool_response({"uuid": "abc"}) == {"uuid": "abc"}

    def test_non_dict_returns_empty(self) -> None:
        assert unwrap_tool_response(None) == {}  # type: ignore[arg-type]
        assert unwrap_tool_response("oops") == {}  # type: ignore[arg-type]

    def test_invalid_inner_json_returns_empty(self) -> None:
        raw = {"result": {"content": [{"text": "not-json"}]}}
        assert unwrap_tool_response(raw) == {}


# --- _post_json ------------------------------------------------------------

class TestPostJson:
    def test_transport_error_returns_structured_failure(self, monkeypatch) -> None:
        from scripts.client import onboard_helper

        def fail(*args, **kwargs):
            raise urllib.error.URLError(PermissionError(1, "Operation not permitted"))

        monkeypatch.setattr(onboard_helper.urllib.request, "urlopen", fail)

        raw = _post_json(
            "http://127.0.0.1:8767/v1/tools/call",
            {"name": "onboard", "arguments": {}},
            timeout=1,
            token=None,
        )
        parsed = unwrap_tool_response(raw)

        assert parsed["success"] is False
        assert parsed["recovery"]["reason"] == "transport_error"
        assert "Operation not permitted" in parsed["error"]


# --- predicates ------------------------------------------------------------

class TestPredicates:
    def test_success_requires_uuid(self) -> None:
        assert is_successful_onboard({"success": True, "uuid": "abc"})
        assert not is_successful_onboard({"success": True})
        assert not is_successful_onboard({"success": False, "uuid": "abc"})

    def test_trajectory_required_detects_recovery_reason(self) -> None:
        parsed = {"success": False, "recovery": {"reason": "trajectory_required"}}
        assert trajectory_required(parsed)

    def test_trajectory_required_only_on_failure(self) -> None:
        assert not trajectory_required({"success": True, "uuid": "abc"})

    def test_trajectory_required_ignores_unknown_reasons(self) -> None:
        parsed = {"success": False, "recovery": {"reason": "something_else"}}
        assert not trajectory_required(parsed)


# --- run_onboard -----------------------------------------------------------

class TestRunOnboard:
    def _call(
        self,
        tmp_path: Path,
        responses: list[dict],
        *,
        initial_cache: dict | None = None,
    ) -> tuple[dict, FakePoster, Path]:
        if initial_cache is not None:
            cache_dir = tmp_path / ".unitares"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "session.json").write_text(json.dumps(initial_cache))
        poster = FakePoster(responses)
        result = run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            post_json=poster,
        )
        return result, poster, tmp_path / ".unitares" / "session.json"

    def test_success_writes_cache(self, tmp_path: Path) -> None:
        result, poster, cache_path = self._call(tmp_path, [_success_response()])

        assert result["status"] == "ok"
        assert result["uuid"] == "uuid-ok"

        assert len(poster.calls) == 1
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["name"] == "acme"
        assert sent_args["force_new"] is True
        assert "parent_agent_id" not in sent_args

        written = json.loads(cache_path.read_text())
        assert written["schema_version"] == 2
        assert written["uuid"] == "uuid-ok"
        assert written["client_session_id"] == "agent-ok"
        assert "updated_at" in written
        # S20.3: continuity_token must NOT be persisted to the cache file.
        # The field stays in the in-process return value (transient) so a
        # caller can use it within the same process, but is never written
        # to disk — lineage across process-instances is declared via
        # parent_agent_id, not resumed via cached token.
        assert "continuity_token" not in written
        assert "continuity_token_supported" not in written
        # Result still carries the transient token from the server response.
        assert result["continuity_token"] == "v1.token-ok"

    def test_cache_file_is_mode_0600(self, tmp_path: Path) -> None:
        """S20.3: cache file must be readable only by the owner.

        Default Path.write_text inherits umask 022 → mode 0644 (world-
        readable on a typical macOS setup). The cache no longer carries
        continuity_token, but client_session_id is still process-instance
        identity — same-UID readability is still a siphon surface.
        """
        _, _, cache_path = self._call(tmp_path, [_success_response()])
        assert cache_path.exists()
        mode = stat.S_IMODE(os.stat(cache_path).st_mode)
        assert mode == 0o600, f"expected mode 0600, got {oct(mode)}"

    def test_write_failure_does_not_leave_tmp_file(self, tmp_path: Path, monkeypatch) -> None:
        """S20.3: a failed atomic write unlinks the temp file rather than
        leaving a .tmp turd in ``.unitares/``."""
        from scripts.client import onboard_helper

        real_replace = os.replace

        def boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(onboard_helper.os, "replace", boom)
        with pytest.raises(OSError):
            onboard_helper._write_cache(tmp_path, {"uuid": "x"}, slot=None)

        cache_dir = tmp_path / ".unitares"
        if cache_dir.exists():
            stragglers = [p for p in cache_dir.iterdir() if p.suffix == ".tmp"]
            assert stragglers == [], f"temp file leaked: {stragglers}"
        # Restore for other tests in the same session.
        monkeypatch.setattr(onboard_helper.os, "replace", real_replace)

    def test_cached_uuid_declared_as_parent_on_startup(self, tmp_path: Path) -> None:
        initial = {
            "uuid": "parent-uuid",
            "continuity_token": "v1.cached-token",
            "client_session_id": "agent-cached",
        }
        _, poster, _ = self._call(
            tmp_path, [_success_response()], initial_cache=initial
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["force_new"] is True
        assert sent_args["parent_agent_id"] == "parent-uuid"
        assert sent_args["spawn_reason"] == "new_session"
        assert "continuity_token" not in sent_args
        assert "client_session_id" not in sent_args

    def test_cached_session_id_is_not_used_for_startup_resume(self, tmp_path: Path) -> None:
        initial = {"continuity_token": "", "client_session_id": "agent-only"}
        _, poster, _ = self._call(
            tmp_path, [_success_response()], initial_cache=initial
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["force_new"] is True
        assert "client_session_id" not in sent_args
        assert "continuity_token" not in sent_args
        assert "parent_agent_id" not in sent_args

    def test_trajectory_required_surfaces_error_without_retry(self, tmp_path: Path) -> None:
        """Per 718ccd3: never auto-retry with a different identity posture."""
        result, poster, cache_path = self._call(
            tmp_path, [_trajectory_required_response()]
        )

        assert result["status"] == "trajectory_required"
        assert "trajectory" in result["error"].lower()
        assert result["recovery_reason"] == "trajectory_required"
        # Must NOT have retried — only one call
        assert len(poster.calls) == 1
        # Must NOT have written cache
        assert not cache_path.exists()

    def test_non_trajectory_failure_does_not_retry(self, tmp_path: Path) -> None:
        failure = {
            "name": "onboard",
            "result": {
                "success": False,
                "error": "Something else broke",
                "recovery": {"reason": "database_unreachable"},
            },
        }
        result, poster, cache_path = self._call(tmp_path, [failure])

        assert result["status"] == "onboard_failed"
        assert len(poster.calls) == 1
        assert not cache_path.exists()

    def test_failure_preserves_existing_cache(self, tmp_path: Path) -> None:
        initial = {
            "uuid": "previous-uuid",
            "agent_id": "Prev",
            "client_session_id": "agent-prev",
            "continuity_token": "v1.prev",
        }
        responses = [_trajectory_required_response()]
        result, _, cache_path = self._call(
            tmp_path, responses, initial_cache=initial
        )

        assert result["status"] == "trajectory_required"
        preserved = json.loads(cache_path.read_text())
        assert preserved == initial, "cache must not be overwritten on failure"

    def test_server_unreachable_no_cache_write(self, tmp_path: Path) -> None:
        # Poster returns {} to simulate network failure.
        result, _, cache_path = self._call(tmp_path, [{}])

        assert result["status"] == "onboard_failed"
        assert not cache_path.exists()

    def test_success_without_uuid_is_failure(self, tmp_path: Path) -> None:
        # This is the specific bug that caused the prod incident:
        # success field missing, uuid missing, but fields silently extracted.
        weird = {"name": "onboard", "result": {"agent_signature": {"uuid": None}}}
        result, _, cache_path = self._call(tmp_path, [weird])
        assert result["status"] == "onboard_failed"
        assert not cache_path.exists()

    def test_explicit_force_new_skips_cache_and_sends_flag(self, tmp_path: Path) -> None:
        """force_new=True remains an explicit way to ignore cached lineage."""
        initial = {
            "uuid": "parent-uuid",
            "continuity_token": "v1.cached-token",
            "client_session_id": "agent-cached",
        }
        cache_dir = tmp_path / ".unitares"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "session.json").write_text(json.dumps(initial))

        poster = FakePoster([_success_response()])
        result = run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            force_new=True,
            post_json=poster,
        )
        assert result["status"] == "ok"
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["force_new"] is True
        # When force_new is explicit, cached lineage and proof material are not sent.
        assert "parent_agent_id" not in sent_args
        assert "continuity_token" not in sent_args
        assert "client_session_id" not in sent_args


# --- thread-anchor resume (UNITARES_CLIENT_SESSION_ID) ---------------------

class TestThreadAnchorResume:
    """When the orchestrator provisions a stable per-conversation
    client_session_id (UNITARES_CLIENT_SESSION_ID), the helper resumes the same
    identity across turns instead of minting a new id every turn — the Discord
    BEAM bridge case.
    """

    def _call(
        self,
        tmp_path: Path,
        responses: list[dict],
        *,
        client_session_id: str | None,
        orchestrated: bool = True,
        force_new: bool = False,
        initial_cache: dict | None = None,
    ) -> tuple[dict, FakePoster]:
        if initial_cache is not None:
            cache_dir = tmp_path / ".unitares"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "session.json").write_text(json.dumps(initial_cache))
        poster = FakePoster(responses)
        result = run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            client_session_id=client_session_id,
            orchestrated=orchestrated,
            force_new=force_new,
            post_json=poster,
        )
        return result, poster

    def test_anchor_resumes_instead_of_force_new(self, tmp_path: Path) -> None:
        _, poster = self._call(
            tmp_path, [_success_response()],
            client_session_id="agent:/thread-discord-42",
        )
        sent_args = poster.calls[0][1]["arguments"]
        # Resume posture: stable anchor sent, NO force_new, NO lineage.
        assert sent_args["client_session_id"] == "agent:/thread-discord-42"
        assert "force_new" not in sent_args
        assert "parent_agent_id" not in sent_args
        assert "spawn_reason" not in sent_args

    def test_anchor_without_orchestrated_flag_mints_not_siphons(self, tmp_path: Path) -> None:
        """THE fail-closed guard: a bare anchor with NO orchestration signal
        (e.g. a leaked global UNITARES_CLIENT_SESSION_ID on a normal session)
        must MINT, never resume-share. This is the property that keeps the
        shared hook from siphoning interactive sessions onto one UUID."""
        _, poster = self._call(
            tmp_path, [_success_response()],
            client_session_id="agent:/leaked-global-anchor",
            orchestrated=False,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["force_new"] is True
        assert "client_session_id" not in sent_args

    def test_anchor_does_not_declare_cached_lineage(self, tmp_path: Path) -> None:
        """Even with a cached UUID, anchor mode resumes (one agent across turns),
        it does NOT declare the cache as a parent."""
        initial = {"uuid": "prior-turn-uuid", "client_session_id": "agent:/thread-1"}
        _, poster = self._call(
            tmp_path, [_success_response()],
            client_session_id="agent:/thread-1",
            initial_cache=initial,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["client_session_id"] == "agent:/thread-1"
        assert "parent_agent_id" not in sent_args
        assert "force_new" not in sent_args

    def test_explicit_force_new_wins_over_anchor(self, tmp_path: Path) -> None:
        """force_new is a deliberate clean break — it overrides the anchor."""
        _, poster = self._call(
            tmp_path, [_success_response()],
            client_session_id="agent:/thread-9",
            force_new=True,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["force_new"] is True
        assert "client_session_id" not in sent_args

    def test_blank_anchor_falls_back_to_default_mint(self, tmp_path: Path) -> None:
        """A blank/whitespace anchor is treated as absent — default fresh mint."""
        _, poster = self._call(
            tmp_path, [_success_response()],
            client_session_id="   ",
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["force_new"] is True
        assert "client_session_id" not in sent_args

    def test_no_anchor_is_byte_identical_to_default(self, tmp_path: Path) -> None:
        """Without the anchor, behavior is unchanged (force_new + no client id)."""
        _, poster = self._call(
            tmp_path, [_success_response()],
            client_session_id=None, orchestrated=False,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["force_new"] is True
        assert "client_session_id" not in sent_args

    def test_anchor_still_attaches_bootstrap_initial_state(self, tmp_path: Path) -> None:
        _, poster = self._call(
            tmp_path, [_success_response()],
            client_session_id="agent:/thread-5",
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert "initial_state" in sent_args

    def test_env_truthy_fails_closed(self) -> None:
        """UNITARES_ORCHESTRATED parsing: only explicit affirmatives are True so
        an empty / 0 / garbage value cannot accidentally arm resume."""
        from scripts.client.onboard_helper import _env_truthy
        assert _env_truthy("1") is True
        assert _env_truthy("true") is True
        assert _env_truthy("YES") is True
        assert _env_truthy("on") is True
        for falsey in (None, "", "0", "false", "no", "off", "  ", "maybe"):
            assert _env_truthy(falsey) is False, falsey


# --- per-process slot isolation --------------------------------------------

class TestSlotIsolation:
    """Slot key (typically Claude Code session_id) isolates parallel processes
    so they don't collide on a single per-workspace cache file. Resolves the
    "multiple Claude agents sharing UUID" symptom flagged 2026-04-14.
    """

    def _call(
        self,
        tmp_path: Path,
        responses: list[dict],
        *,
        slot: str | None,
        initial_files: dict[str, dict] | None = None,
    ) -> tuple[dict, FakePoster, Path]:
        cache_dir = tmp_path / ".unitares"
        if initial_files:
            cache_dir.mkdir(parents=True, exist_ok=True)
            for filename, payload in initial_files.items():
                (cache_dir / filename).write_text(json.dumps(payload))
        poster = FakePoster(responses)
        result = run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            slot=slot,
            post_json=poster,
        )
        from scripts.client.onboard_helper import _slot_filename
        return result, poster, cache_dir / _slot_filename(slot)

    def test_slot_writes_to_distinct_file(self, tmp_path: Path) -> None:
        result, _, cache_path = self._call(
            tmp_path, [_success_response(uuid="u-A", session_id="agent-A")],
            slot="claude-session-aaa",
        )
        assert result["status"] == "ok"
        assert cache_path.name == "session-claude-session-aaa.json"
        assert cache_path.exists()
        # Default cache file must NOT be touched.
        assert not (tmp_path / ".unitares" / "session.json").exists()

    def test_two_slots_do_not_collide(self, tmp_path: Path) -> None:
        # Process A
        result_a, _, path_a = self._call(
            tmp_path, [_success_response(uuid="u-A", session_id="agent-A")],
            slot="aaa",
        )
        # Process B starts in same workspace, different slot — must not see A's cache
        result_b, poster_b, path_b = self._call(
            tmp_path, [_success_response(uuid="u-B", session_id="agent-B")],
            slot="bbb",
        )
        assert result_a["uuid"] == "u-A"
        assert result_b["uuid"] == "u-B"
        assert path_a != path_b
        # Process B onboarded fresh — it didn't pick up A's continuity token.
        sent_args_b = poster_b.calls[0][1]["arguments"]
        assert "continuity_token" not in sent_args_b
        assert "client_session_id" not in sent_args_b

    def test_slot_falls_back_to_legacy_cache_for_lineage_if_no_slot_file_yet(self, tmp_path: Path) -> None:
        # A pre-existing unslotted cache (from before this change) should still
        # provide a lineage candidate to a slotted run on first start. Slotted
        # writes then own that slot going forward.
        legacy = {
            "uuid": "legacy-parent-uuid",
            "continuity_token": "v1.legacy-token",
            "client_session_id": "agent-legacy",
        }
        result, poster, cache_path = self._call(
            tmp_path, [_success_response(uuid="u-resumed", session_id="agent-resumed")],
            slot="first-run",
            initial_files={"session.json": legacy},
        )
        assert result["status"] == "ok"
        sent_args = poster.calls[0][1]["arguments"]
        # Declared lineage via legacy cache UUID; did not resume via token.
        assert sent_args["force_new"] is True
        assert sent_args["parent_agent_id"] == "legacy-parent-uuid"
        assert sent_args["spawn_reason"] == "new_session"
        assert "continuity_token" not in sent_args
        assert "client_session_id" not in sent_args
        # New write went to the slotted file, not back to session.json
        assert cache_path.name == "session-first-run.json"
        # Legacy file is untouched
        legacy_path = tmp_path / ".unitares" / "session.json"
        assert legacy_path.exists()
        assert json.loads(legacy_path.read_text()) == legacy

    def test_new_slot_uses_newest_existing_slotted_cache_for_lineage(self, tmp_path: Path) -> None:
        """A fresh process usually has a fresh slot. It should still see
        workspace lineage from the newest existing session cache, not only
        from legacy session.json."""
        older = {
            "uuid": "older-parent",
            "updated_at": "2026-05-01T00:00:00+00:00",
        }
        newer = {
            "uuid": "newer-parent",
            "updated_at": "2026-05-02T00:00:00+00:00",
        }
        result, poster, cache_path = self._call(
            tmp_path,
            [_success_response(uuid="child-uuid", session_id="agent-child")],
            slot="fresh-process",
            initial_files={
                "session-older.json": older,
                "session-newer.json": newer,
            },
        )

        assert result["status"] == "ok"
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["parent_agent_id"] == "newer-parent"
        assert sent_args["spawn_reason"] == "new_session"
        assert cache_path.name == "session-fresh-process.json"

    def test_exact_slot_cache_wins_over_newer_other_slot(self, tmp_path: Path) -> None:
        exact = {
            "uuid": "exact-parent",
            "updated_at": "2026-05-01T00:00:00+00:00",
        }
        newer_other = {
            "uuid": "other-parent",
            "updated_at": "2026-05-02T00:00:00+00:00",
        }
        _, poster, _ = self._call(
            tmp_path,
            [_success_response(uuid="child-uuid", session_id="agent-child")],
            slot="same-process",
            initial_files={
                "session-same-process.json": exact,
                "session-other-process.json": newer_other,
            },
        )

        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["parent_agent_id"] == "exact-parent"

    def test_newest_cache_without_uuid_does_not_hide_older_lineage(self, tmp_path: Path) -> None:
        uuid_less = {
            "client_session_id": "agent-no-uuid",
            "updated_at": "2026-05-03T00:00:00+00:00",
        }
        older_with_uuid = {
            "uuid": "usable-parent",
            "updated_at": "2026-05-02T00:00:00+00:00",
        }
        _, poster, _ = self._call(
            tmp_path,
            [_success_response(uuid="child-uuid", session_id="agent-child")],
            slot="fresh-process",
            initial_files={
                "session-no-uuid.json": uuid_less,
                "session-usable.json": older_with_uuid,
            },
        )

        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["parent_agent_id"] == "usable-parent"

    def test_unsanitized_slot_filename_chars_are_replaced(self, tmp_path: Path) -> None:
        # Slot keys come from external input (Claude Code session_id) — must
        # not allow path traversal via "../" or weird chars.
        from scripts.client.onboard_helper import _slot_filename
        assert _slot_filename("normal-id-123") == "session-normal-id-123.json"
        assert _slot_filename("../../etc/passwd") == "session-______etc_passwd.json"
        assert "/" not in _slot_filename("a/b/c")
        # Cap length so attackers can't fill the disk with a giant filename.
        long = "x" * 500
        assert len(_slot_filename(long)) <= len("session-.json") + 64

    def test_no_slot_uses_legacy_path_unchanged(self, tmp_path: Path) -> None:
        # Backward-compat: when slot is None, the cache path is the original
        # session.json — same as the pre-slotted behavior.
        from scripts.client.onboard_helper import _slot_filename
        assert _slot_filename(None) == "session.json"
        assert _slot_filename("") == "session.json"


# --- Phase 4: bootstrap initial_state wiring -------------------------------

class TestBootstrapInitialState:
    """Phase 4 of onboard-bootstrap-checkin.md — the SessionStart hook
    wires initial_state into the onboard call. Tests live here in the
    onboard helper test file because the helper owns the wiring."""

    def test_default_attaches_initial_state(self, tmp_path: Path) -> None:
        """Hook-driven onboards default to with_bootstrap=True so the server
        writes a t=0 anchor."""
        poster = FakePoster([_success_response()])
        run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            post_json=poster,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert "initial_state" in sent_args
        assert sent_args["initial_state"]["task_type"] == "introspection"
        # Hook MUST NOT fabricate confidence/complexity from session metadata —
        # let the server fill its 0.5 defaults.
        assert "confidence" not in sent_args["initial_state"]
        assert "complexity" not in sent_args["initial_state"]

    def test_with_bootstrap_false_omits_initial_state(self, tmp_path: Path) -> None:
        """Explicit opt-out for callers like /governance-start that want
        no bootstrap row."""
        poster = FakePoster([_success_response()])
        run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            with_bootstrap=False,
            post_json=poster,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert "initial_state" not in sent_args

    def test_initial_state_present_with_declared_lineage(self, tmp_path: Path) -> None:
        """Declared-lineage startup still sends initial_state so the new
        identity gets its t=0 anchor."""
        initial = {
            "uuid": "parent-uuid",
            "continuity_token": "v1.cached-token",
            "client_session_id": "agent-cached",
        }
        cache_dir = tmp_path / ".unitares"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "session.json").write_text(json.dumps(initial))

        poster = FakePoster([_success_response()])
        run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            post_json=poster,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args["parent_agent_id"] == "parent-uuid"
        assert "continuity_token" not in sent_args
        assert "initial_state" in sent_args

    def test_initial_state_present_with_force_new(self, tmp_path: Path) -> None:
        """force_new=True still attaches initial_state — the new identity
        gets its own t=0 anchor per spec §3.4."""
        poster = FakePoster([_success_response()])
        run_onboard(
            server_url="http://fake",
            agent_name="acme",
            model_type="claude-code",
            workspace=tmp_path,
            force_new=True,
            post_json=poster,
        )
        sent_args = poster.calls[0][1]["arguments"]
        assert sent_args.get("force_new") is True
        assert "initial_state" in sent_args

    def test_initial_state_payload_shape_is_minimal(self, tmp_path: Path) -> None:
        """The hook-built initial_state contains exactly task_type. Anything
        beyond that — response_text, complexity, confidence, ethical_drift —
        is left to the server's defaults so the hook stays honest about
        what it actually knows at session-start."""
        from scripts.client.onboard_helper import _build_bootstrap_initial_state

        payload = _build_bootstrap_initial_state()
        assert set(payload.keys()) == {"task_type"}
        assert payload["task_type"] == "introspection"
