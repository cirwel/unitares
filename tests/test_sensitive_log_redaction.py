"""Sensitive session and identity material must not be copied into logs."""

import logging
from unittest.mock import AsyncMock

import pytest

from src.cache import session_cache as cache_module
from src.mcp_handlers.identity import session as identity_session


@pytest.mark.asyncio
async def test_session_cache_bind_logs_backend_without_identifiers(
    monkeypatch, caplog
):
    session_secret = "session-proof-that-must-never-reach-logs"
    agent_identifier = "agent-identifier-that-must-never-reach-logs"
    monkeypatch.setattr(cache_module, "get_redis", AsyncMock(return_value=None))

    with caplog.at_level(logging.DEBUG, logger=cache_module.logger.name):
        await cache_module.SessionCache().bind(session_secret, agent_identifier)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Session bound in memory" in messages
    assert session_secret not in messages
    assert agent_identifier not in messages
    cache_module._fallback_cache.pop(session_secret, None)


def test_client_session_sanitization_logs_shape_not_values(caplog):
    caller_value = "caller/secret?with-control"
    expected_sanitized = "caller_secret_with-control"

    with caplog.at_level(logging.WARNING, logger=identity_session.logger.name):
        normalized = identity_session.normalize_client_session_id(caller_value)

    assert normalized == expected_sanitized
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "client_session_id sanitized" in messages
    assert caller_value not in messages
    assert expected_sanitized not in messages


def test_fingerprint_debug_log_omits_write_proof(caplog):
    write_proof = "203.0.113.10:sensitive-ua-hash:rotating-suffix"

    with caplog.at_level(logging.DEBUG, logger=identity_session.logger.name):
        result = identity_session._extract_base_fingerprint(write_proof)

    assert result == "ua:sensitive-ua-hash"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "extracted rotating-transport fingerprint" in messages
    assert write_proof not in messages
    assert "sensitive-ua-hash" not in messages
