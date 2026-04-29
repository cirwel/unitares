"""Strict-by-default identity gate configuration."""


def test_identity_strict_mode_defaults_to_strict(monkeypatch):
    monkeypatch.delenv("UNITARES_IDENTITY_STRICT", raising=False)

    from config.governance_config import identity_strict_mode

    assert identity_strict_mode() == "strict"


def test_identity_strict_mode_invalid_env_falls_back_to_strict(monkeypatch):
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "bogus")

    from config.governance_config import identity_strict_mode

    assert identity_strict_mode() == "strict"


def test_session_fingerprint_check_defaults_to_strict(monkeypatch):
    monkeypatch.delenv("UNITARES_SESSION_FINGERPRINT_CHECK", raising=False)

    from config.governance_config import session_fingerprint_check_mode

    assert session_fingerprint_check_mode() == "strict"


def test_session_fingerprint_invalid_env_falls_back_to_strict(monkeypatch):
    monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "bogus")

    from config.governance_config import session_fingerprint_check_mode

    assert session_fingerprint_check_mode() == "strict"
