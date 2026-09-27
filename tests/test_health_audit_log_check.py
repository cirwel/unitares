"""The deep-health audit-log check separates "nothing written yet" from a fault.

A new install has written no audit event, so its JSONL log does not exist yet.
That is informational (``no_data_yet``), not a warning; only a log that can
never be created (its directory is missing or not writable) is one. With JSONL
writes switched off there is no file to expect (``not_configured``).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.services.runtime_queries import (
    NEUTRAL_STATUSES,
    NO_DATA_YET,
    NOT_CONFIGURED,
    _audit_log_check,
)


def _logger(path, jsonl_enabled=True):
    return SimpleNamespace(log_file=path, _jsonl_enabled=jsonl_enabled)


def test_existing_log_is_healthy(tmp_path):
    log = tmp_path / "audit_log.jsonl"
    log.write_text("{}\n")
    assert _audit_log_check(_logger(log)) == {"status": "healthy", "audit_log_exists": True}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_existing_read_only_log_is_a_warning(tmp_path):
    log = tmp_path / "audit_log.jsonl"
    log.write_text("{}\n")
    log.chmod(0o400)
    try:
        check = _audit_log_check(_logger(log))
    finally:
        log.chmod(0o600)
    assert check["status"] == "warning"
    assert check["audit_log_exists"] is True


def test_log_path_that_is_a_directory_is_a_warning(tmp_path):
    log = tmp_path / "audit_log.jsonl"
    log.mkdir()
    check = _audit_log_check(_logger(log))
    assert check["status"] == "warning"
    assert "not an appendable file" in check["warning"]


def test_absent_log_in_a_writable_directory_is_no_data_yet(tmp_path):
    check = _audit_log_check(_logger(tmp_path / "audit_log.jsonl"))
    assert check["status"] == NO_DATA_YET
    assert check["audit_log_exists"] is False
    assert "warning" not in check


def test_absent_log_whose_directory_is_missing_is_a_warning(tmp_path):
    check = _audit_log_check(_logger(tmp_path / "gone" / "audit_log.jsonl"))
    assert check["status"] == "warning"
    assert "not writable" in check["warning"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_absent_log_whose_directory_is_read_only_is_a_warning(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        check = _audit_log_check(_logger(ro / "audit_log.jsonl"))
    finally:
        ro.chmod(0o700)
    assert check["status"] == "warning"


def test_jsonl_writes_off_is_not_configured(tmp_path):
    check = _audit_log_check(_logger(tmp_path / "audit_log.jsonl", jsonl_enabled=False))
    assert check["status"] == NOT_CONFIGURED
    assert "UNITARES_AUDIT_WRITE_JSONL" in check["note"]


def test_neutral_statuses_are_never_healthy_or_faults():
    assert NEUTRAL_STATUSES == {NOT_CONFIGURED, NO_DATA_YET}
    assert not NEUTRAL_STATUSES & {"healthy", "warning", "degraded", "error", "unavailable"}
