"""Canary for the explicit-``agent_id`` REST bind path.

`_bind_explicit_http_agent` accepts an identity on a *shape* check alone — 36
characters, 4 hyphens — with no lookup and no proof of ownership, and it runs
first in `_resolve_http_prebind`. A resolution that succeeds there means the
strict-identity gate downstream never sees a miss and never emits its typed
refusal.

These tests pin two things:

1. the canary classifies corroboration correctly, so the measurement it feeds
   can be trusted;
2. the canary changed no behaviour — the same inputs bind exactly as before.

The second is the important one. A canary that quietly alters what binds is
worse than no canary, because the population it reports would be a population
it created.
"""

from src.http_api import _bind_explicit_http_agent, _explicit_bind_corroboration

VALID = "00000000-0000-0000-0000-000000000000"


class TestCorroborationClassifier:
    def test_uuid_alone_is_none(self):
        # The population a corroboration requirement would turn away.
        assert _explicit_bind_corroboration({"agent_id": VALID}) == "none"

    def test_echoed_client_session_id_is_csid(self):
        # The server issued this, so the caller demonstrably onboarded.
        assert (
            _explicit_bind_corroboration({"agent_id": VALID, "client_session_id": "c"}) == "csid"
        )

    def test_continuity_token_is_token(self):
        assert _explicit_bind_corroboration({"agent_id": VALID, "continuity_token": "t"}) == "token"

    def test_csid_wins_when_both_present(self):
        args = {"client_session_id": "c", "continuity_token": "t"}
        assert _explicit_bind_corroboration(args) == "csid"

    def test_empty_values_do_not_count_as_corroboration(self):
        # "" is not proof. Counting it would understate the population that
        # breaks when the path is tightened — the exact number this exists for.
        assert _explicit_bind_corroboration({"client_session_id": ""}) == "none"
        assert _explicit_bind_corroboration({"continuity_token": ""}) == "none"

    def test_non_string_values_do_not_count(self):
        assert _explicit_bind_corroboration({"client_session_id": 123}) == "none"

    def test_non_dict_is_none(self):
        assert _explicit_bind_corroboration(None) == "none"
        assert _explicit_bind_corroboration("nope") == "none"


class TestBindBehaviourUnchanged:
    """The canary observes. It must not decide."""

    def test_shape_valid_uuid_still_binds(self):
        assert _bind_explicit_http_agent({"agent_id": VALID}) == VALID

    def test_binds_with_no_corroboration_exactly_as_before(self):
        # This is the case the audit is about. It must still bind, or the
        # canary would be a silent fix and the measurement meaningless.
        assert _bind_explicit_http_agent({"agent_id": VALID}) == VALID

    def test_wrong_length_still_rejected(self):
        assert _bind_explicit_http_agent({"agent_id": "too-short"}) is None

    def test_wrong_hyphen_count_still_rejected(self):
        assert _bind_explicit_http_agent({"agent_id": "0" * 36}) is None

    def test_missing_agent_id_still_returns_none(self):
        assert _bind_explicit_http_agent({}) is None

    def test_non_string_agent_id_still_returns_none(self):
        assert _bind_explicit_http_agent({"agent_id": 12345}) is None


class TestCanaryLogHygiene:
    def test_log_line_never_contains_the_uuid(self, caplog):
        # A prefix is still an identity fragment. This line is meant to be
        # safe to leave on in a live server and safe to read in a shared log.
        with caplog.at_level("INFO"):
            _bind_explicit_http_agent({"agent_id": VALID})

        emitted = " ".join(r.getMessage() for r in caplog.records)
        assert "[ATTEST]" in emitted
        assert VALID not in emitted
        assert VALID[:8] not in emitted

    def test_log_line_reports_the_corroboration_class(self, caplog):
        with caplog.at_level("INFO"):
            _bind_explicit_http_agent({"agent_id": VALID, "client_session_id": "c"})

        emitted = " ".join(r.getMessage() for r in caplog.records)
        assert "corroboration=csid" in emitted
