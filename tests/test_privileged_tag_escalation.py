"""Privilege-escalation guards on caller-supplied agent tags and thresholds.

Regression cover for the 2026-08-05 trust-anchor audit. `agent(action=update)`
accepted `tags` as an unvalidated List[Any] and wrote it verbatim, so an agent
could grant itself capability tags -- `admin` (fleet-global set_thresholds),
`embodied` (R4 substrate -> tier 3), `persistent`/`autonomous` (archival and
loop-detection exemptions). Separately, set_thresholds accepted a
`total_updates >= 100` "high reputation" route, and total_updates is a raw
check-in counter.
"""

import pytest
import json
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import make_agent_meta, make_mock_server, patch_lifecycle_server, patch_agent_storage


def _parse(result):
    return json.loads(result[0].text)


def _update_patches(server):
    return (
        patch_lifecycle_server(server, require_registered=("agent-1", None)),
        patch_agent_storage(),
        patch("src.mcp_handlers.identity.shared.require_write_permission", return_value=(True, None)),
        patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True),
    )


class TestPrivilegedTagGuard:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tag", ["admin", "embodied", "persistent", "autonomous", "protected", "pioneer", "anima"])
    async def test_cannot_self_assign_privileged_tag(self, tag):
        """Each capability tag must be refused when the agent does not hold it."""
        server = make_mock_server()
        meta = make_agent_meta(tags=["ephemeral"])
        server.agent_metadata = {"agent-1": meta}

        p1, p2, p3, p4 = _update_patches(server)
        with p1, p2 as mock_storage, p3, p4:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            from src.mcp_handlers.lifecycle.handlers import handle_update_agent_metadata
            result = await handle_update_agent_metadata({
                "agent_id": "agent-1", "tags": ["ephemeral", tag],
            })
            data = _parse(result)
            assert data.get("success") is not True
            assert tag in json.dumps(data)
            # The write must not have landed.
            assert meta.tags == ["ephemeral"]

    @pytest.mark.asyncio
    async def test_ordinary_tags_still_writable(self):
        """The guard must not block normal descriptive tagging."""
        server = make_mock_server()
        meta = make_agent_meta(tags=["ephemeral"])
        server.agent_metadata = {"agent-1": meta}

        p1, p2, p3, p4 = _update_patches(server)
        with p1, p2 as mock_storage, p3, p4:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            from src.mcp_handlers.lifecycle.handlers import handle_update_agent_metadata
            result = await handle_update_agent_metadata({
                "agent_id": "agent-1", "tags": ["explorer", "governance"],
            })
            data = _parse(result)
            assert data["success"] is True
            assert meta.tags == ["explorer", "governance"]

    @pytest.mark.asyncio
    async def test_held_privileged_tags_are_preserved_not_stripped(self):
        """`tags` is a whole-list replace, so a resident re-sending its own tags
        must not be de-privileged -- otherwise updating notes would silently
        strip Lumen's 'persistent'/'autonomous' and expose it to auto-archival.
        """
        server = make_mock_server()
        meta = make_agent_meta(tags=["persistent", "autonomous"])
        server.agent_metadata = {"agent-1": meta}

        p1, p2, p3, p4 = _update_patches(server)
        with p1, p2 as mock_storage, p3, p4:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            from src.mcp_handlers.lifecycle.handlers import handle_update_agent_metadata
            result = await handle_update_agent_metadata({
                "agent_id": "agent-1", "tags": ["persistent", "autonomous", "explorer"],
            })
            data = _parse(result)
            assert data["success"] is True
            assert set(meta.tags) == {"persistent", "autonomous", "explorer"}

    @pytest.mark.asyncio
    async def test_holding_one_privileged_tag_does_not_grant_another(self):
        """A resident with 'persistent' still cannot escalate to 'admin'."""
        server = make_mock_server()
        meta = make_agent_meta(tags=["persistent"])
        server.agent_metadata = {"agent-1": meta}

        p1, p2, p3, p4 = _update_patches(server)
        with p1, p2 as mock_storage, p3, p4:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            from src.mcp_handlers.lifecycle.handlers import handle_update_agent_metadata
            result = await handle_update_agent_metadata({
                "agent_id": "agent-1", "tags": ["persistent", "admin"],
            })
            data = _parse(result)
            assert data.get("success") is not True
            assert "admin" in json.dumps(data)
            assert meta.tags == ["persistent"]

    def test_privileged_tag_set_covers_every_live_consumer(self):
        """Guard the guard: if a new capability tag is wired into a consumer,
        it must be added to PRIVILEGED_TAGS or this test should be updated
        deliberately."""
        from src.mcp_handlers.lifecycle.mutation import PRIVILEGED_TAGS
        from src.agent_lifecycle import _PROTECTED_TIERS  # noqa: F401  (import guard)

        # admin -> set_thresholds; embodied -> R4 substrate; the rest -> archival
        # immunity / loop-detection / stuck-sweep exemptions.
        for tag in ("admin", "embodied", "persistent", "autonomous"):
            assert tag in PRIVILEGED_TAGS
        # 'ephemeral' is descriptive, must stay writable.
        assert "ephemeral" not in PRIVILEGED_TAGS


class TestThresholdRouteRemoved:
    """set_thresholds must be admin-tag-only.

    The removed `total_updates >= 100` route measured raw check-in count, which
    increments unconditionally on every update -- so ~100 check-ins (about two
    minutes under the 60/min limiter) bought write access to fleet-global
    governance parameters.
    """

    @pytest.mark.asyncio
    async def test_high_update_count_alone_cannot_set_thresholds(self):
        server = make_mock_server()
        meta = make_agent_meta(tags=["ephemeral"], total_updates=100_000)
        server.agent_metadata = {"agent-1": meta}

        with patch("src.mcp_handlers.admin.config.mcp_server", server), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.admin.config import handle_set_thresholds
            result = await handle_set_thresholds({
                "agent_id": "agent-1",
                "thresholds": {"risk_approve_threshold": 0.99},
            })
            data = _parse(result)
            assert data.get("success") is not True
            assert "admin" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_admin_tag_still_permitted(self, tmp_path):
        from src.audit_log import AuditLogger
        from src.runtime_config import clear_overrides, get_thresholds

        server = make_mock_server()
        meta = make_agent_meta(tags=["admin"], total_updates=1)
        server.agent_metadata = {"agent-1": meta}
        log_file = tmp_path / "audit_log.jsonl"
        test_logger = AuditLogger(log_file=log_file)

        clear_overrides()
        try:
            with patch("src.mcp_handlers.admin.config.mcp_server", server), \
                 patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
                 patch("src.audit_log.audit_logger", test_logger), \
                 patch.object(test_logger, "_schedule_postgres_write"):
                from src.mcp_handlers.admin.config import handle_set_thresholds
                keyed_csid = "agent-11111111-222-aaaaaaaaaaaaaaaaaaaa"
                result = await handle_set_thresholds({
                    "agent_id": "agent-1",
                    "client_session_id": keyed_csid,
                    "thresholds": {"void_threshold_initial": 0.22},
                })
                body = _parse(result)
                assert body["success"] is True
                data = body.get("data", body)
                assert data["success"] is True
                assert data["updated"] == ["void_threshold_initial"]
                assert data["errors"] == []
                assert data["persistence"] == "process_memory_only"
                assert data["survives_restart"] is False
                assert "process" in data["persistence_note"]
                assert get_thresholds()["void_threshold_initial"] == 0.22

                rows = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
                assert [r["event_type"] for r in rows] == [
                    "threshold_modification_attempt",
                    "threshold_modification_success",
                ]
                for row in rows:
                    assert row["agent_id"] == "agent-1"
                    assert row["confidence"] == 0.0
                    assert row["session_id"].startswith("csid:")
                    assert row["session_id"] != keyed_csid
                assert rows[0]["details"] == {
                    "thresholds": {"void_threshold_initial": 0.22},
                    "validate": True,
                }
                assert rows[1]["details"] == {"updated": ["void_threshold_initial"]}
        finally:
            clear_overrides()

    @pytest.mark.asyncio
    async def test_admin_invalid_threshold_logs_attempt_only_and_leaves_overrides_unchanged(self, tmp_path):
        from src.audit_log import AuditLogger
        from src.runtime_config import clear_overrides, describe_thresholds

        server = make_mock_server()
        meta = make_agent_meta(tags=["admin"], total_updates=1)
        server.agent_metadata = {"agent-1": meta}
        log_file = tmp_path / "audit_log.jsonl"
        test_logger = AuditLogger(log_file=log_file)

        clear_overrides()
        try:
            with patch("src.mcp_handlers.admin.config.mcp_server", server), \
                 patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
                 patch("src.audit_log.audit_logger", test_logger), \
                 patch.object(test_logger, "_schedule_postgres_write"):
                from src.mcp_handlers.admin.config import handle_set_thresholds
                result = await handle_set_thresholds({
                    "agent_id": "agent-1",
                    "thresholds": {
                        "unknown_threshold": 0.5,
                        "risk_approve_threshold": True,
                        "void_threshold_initial": float("nan"),
                        "coherence_critical_threshold": float("inf"),
                    },
                })
                body = _parse(result)
                data = body.get("data", body)
                assert data["success"] is False
                assert data["updated"] == []
                assert len(data["errors"]) == 4
                assert data["persistence"] == "process_memory_only"
                assert data["survives_restart"] is False
                assert all(
                    d["source"] == "class_default"
                    for d in describe_thresholds().values()
                )

                def _reject_non_strict(constant: str):
                    raise ValueError(f"Non-strict JSON constant emitted: {constant}")

                rows = [
                    json.loads(line, parse_constant=_reject_non_strict)
                    for line in log_file.read_text().splitlines()
                    if line.strip()
                ]
                assert [r["event_type"] for r in rows] == ["threshold_modification_attempt"]
                assert rows[0]["confidence"] == 0.0
                assert rows[0]["details"]["thresholds"]["void_threshold_initial"] == "nan"
                assert rows[0]["details"]["thresholds"]["coherence_critical_threshold"] == "inf"
        finally:
            clear_overrides()

    @pytest.mark.asyncio
    async def test_admin_non_dict_thresholds_returns_invalid_param(self, tmp_path):
        from src.audit_log import AuditLogger

        server = make_mock_server()
        meta = make_agent_meta(tags=["admin"], total_updates=1)
        server.agent_metadata = {"agent-1": meta}
        log_file = tmp_path / "audit_log.jsonl"
        test_logger = AuditLogger(log_file=log_file)

        with patch("src.mcp_handlers.admin.config.mcp_server", server), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.audit_log.audit_logger", test_logger), \
             patch.object(test_logger, "_schedule_postgres_write"):
            from src.mcp_handlers.admin.config import handle_set_thresholds
            result = await handle_set_thresholds({
                "agent_id": "agent-1",
                "thresholds": ["not", "a", "dict"],
            })
            body = _parse(result)
            assert body.get("success") is not True
            assert body.get("error_code") == "INVALID_PARAM"
            assert not log_file.exists() or log_file.read_text().strip() == ""

    @pytest.mark.asyncio
    async def test_admin_partial_batch_logs_applied_override_and_errors(self, tmp_path):
        from src.audit_log import AuditLogger
        from src.runtime_config import clear_overrides, get_thresholds

        server = make_mock_server()
        meta = make_agent_meta(tags=["admin"], total_updates=1)
        server.agent_metadata = {"agent-1": meta}
        log_file = tmp_path / "audit_log.jsonl"
        test_logger = AuditLogger(log_file=log_file)

        clear_overrides()
        try:
            with patch("src.mcp_handlers.admin.config.mcp_server", server), \
                 patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
                 patch("src.audit_log.audit_logger", test_logger), \
                 patch.object(test_logger, "_schedule_postgres_write"):
                from src.mcp_handlers.admin.config import handle_set_thresholds
                result = await handle_set_thresholds({
                    "agent_id": "agent-1",
                    "thresholds": {
                        "void_threshold_initial": 0.25,
                        "unknown_threshold": 0.5,
                    },
                })
                body = _parse(result)
                data = body.get("data", body)
                assert data["success"] is False
                assert data["updated"] == ["void_threshold_initial"]
                assert len(data["errors"]) == 1
                assert data["current_thresholds"]["void_threshold_initial"] == 0.25
                assert get_thresholds()["void_threshold_initial"] == 0.25

                rows = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
                assert [r["event_type"] for r in rows] == [
                    "threshold_modification_attempt",
                    "threshold_modification_success",
                ]
                assert rows[1]["details"]["updated"] == ["void_threshold_initial"]
                assert len(rows[1]["details"]["errors"]) == 1
        finally:
            clear_overrides()

    @pytest.mark.asyncio
    async def test_threshold_audit_rows_ignored_by_dialectic_calibration(self, tmp_path):
        from datetime import datetime
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from src.audit_log import AuditLogger
        from src.runtime_config import clear_overrides
        from src.mcp_handlers.dialectic.calibration import (
            update_calibration_from_dialectic,
            update_calibration_from_dialectic_disagreement,
        )

        server = make_mock_server()
        meta = make_agent_meta(tags=["admin"], total_updates=1)
        server.agent_metadata = {"agent-1": meta}
        log_file = tmp_path / "audit_log.jsonl"
        test_logger = AuditLogger(log_file=log_file)

        clear_overrides()
        try:
            with patch("src.mcp_handlers.admin.config.mcp_server", server), \
                 patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
                 patch("src.audit_log.audit_logger", test_logger), \
                 patch.object(test_logger, "_schedule_postgres_write"):
                from src.mcp_handlers.admin.config import handle_set_thresholds
                await handle_set_thresholds({
                    "agent_id": "agent-1",
                    "thresholds": {"void_threshold_initial": 0.21},
                })

            fake_session = SimpleNamespace(
                session_id="sess-cal-1",
                dispute_type="verification",
                paused_agent_id="agent-1",
                reviewer_agent_id="reviewer-2",
                discovery_id=None,
                created_at=datetime.now(),
            )
            fake_resolution = SimpleNamespace(action="resume", timestamp=datetime.now())
            fake_authorship = SimpleNamespace(is_invalid=False, recuses=lambda *_a, **_kw: False)
            mock_cal = MagicMock()

            with patch("src.mcp_handlers.dialectic.calibration.audit_logger", test_logger), \
                 patch("src.mcp_handlers.dialectic.calibration.calibration_checker", mock_cal), \
                 patch("src.mcp_handlers.dialectic.calibration.read_reviewed_work_authorship", return_value=fake_authorship):
                updated = await update_calibration_from_dialectic(fake_session, fake_resolution)
                assert updated is False
                mock_cal.record_prediction.assert_not_called()
                disagreement_logged = await update_calibration_from_dialectic_disagreement(fake_session)
                assert disagreement_logged is False
        finally:
            clear_overrides()

    def test_reputation_route_not_reintroduced(self):
        """Source guard: the escalation was a single boolean. If someone
        reinstates a count-based route, this fails loudly."""
        import inspect
        from src.mcp_handlers.admin import config as config_mod

        # Comments deliberately name the removed route to explain why it went;
        # only executable lines should be free of it.
        code = "\n".join(
            line for line in inspect.getsource(config_mod.handle_set_thresholds).splitlines()
            if not line.strip().startswith("#")
        )
        assert "is_high_reputation" not in code
        assert "total_updates >=" not in code
