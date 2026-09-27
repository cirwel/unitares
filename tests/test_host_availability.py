"""Provider-side cooldowns for the subscription-CLI host adapters."""

from datetime import datetime

import pytest

from src.mcp_handlers.support import host_availability as ha

CODEX_LIMIT = (
    "Codex app-server reported: You’ve hit your usage limit. Visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits or try "
    "again at Sep 29th, 2026 9:10 PM."
)


@pytest.fixture(autouse=True)
def _fresh_state():
    ha._reset_for_tests()
    yield
    ha._reset_for_tests()


def test_codex_usage_limit_is_quota_with_its_stated_reset_time():
    now = datetime(2026, 9, 26, 12, 0).timestamp()
    got = ha.classify(CODEX_LIMIT, now=now)
    assert got["reason"] == "quota"
    assert got["stated_reset"] == datetime(2026, 9, 29, 21, 10).timestamp()


def test_claude_epoch_and_clock_reset_formats():
    now = datetime(2026, 9, 26, 12, 0).timestamp()
    epoch = ha.classify("Claude AI usage limit reached|1790000000", now=now)
    assert epoch == {"reason": "quota", "stated_reset": 1790000000.0}
    clock = ha.classify("5-hour limit reached ∙ resets 3pm", now=now)
    assert clock["stated_reset"] == datetime(2026, 9, 26, 15, 0).timestamp()
    past = ha.classify("5-hour limit reached ∙ resets 9:30am", now=now)
    assert past["stated_reset"] == datetime(2026, 9, 27, 9, 30).timestamp()  # tomorrow


def test_auth_failures_and_unrecognised_errors():
    assert ha.classify("Not logged in · Please run /login")["reason"] == "auth"
    assert ha.classify("You are not logged into Antigravity.")["reason"] == "auth"
    # Unrecognised: never takes a host out of rotation.
    assert ha.classify("Host CLI returned a malformed terminal answer envelope") is None
    assert ha.classify("Antigravity CLI exited 1") is None
    assert ha.classify("") is None


def test_a_stated_reset_is_honoured_but_capped():
    now = 1_000_000.0
    soon = ha.record_unavailable("codex:host-adapter",
                                 {"reason": "quota", "stated_reset": now + 3600}, now=now)
    assert soon["retry_after_source"] == "provider"
    assert ha.cooldown("codex:host-adapter", now=now + 3599) is not None
    assert ha.cooldown("codex:host-adapter", now=now + 3601) is None
    far = ha.record_unavailable("codex:host-adapter",
                                {"reason": "quota", "stated_reset": now + 3 * 86400}, now=now)
    # Three days away is re-checked after the cap, so a lifted limit is noticed.
    assert ha.cooldown("codex:host-adapter", now=now + ha.STATED_RESET_CAP_S + 1) is None
    assert far["retry_after_source"] == "provider"


def test_without_a_stated_reset_the_backoff_doubles_and_caps():
    now = 1_000_000.0
    spans = []
    for _ in range(6):
        # Each failure lands after the previous cooldown lapsed (a re-probe).
        ha.record_unavailable("claude:host-adapter", {"reason": "auth", "stated_reset": None},
                              now=now)
        retry_after = ha._state["claude:host-adapter"]["retry_after"]
        spans.append(retry_after - now)
        now = retry_after + 1
    assert spans[:4] == [1800, 3600, 7200, 14400]
    assert spans[-1] == ha.BACKOFF_CAP_S


def test_concurrent_failures_in_one_window_back_off_once():
    """Review of #2486 (P3): three in-flight calls failing on one outage made
    a 2 h cooldown instead of 30 min."""
    now = 1_000_000.0
    for i in range(3):
        ha.record_unavailable("codex:host-adapter", {"reason": "auth", "stated_reset": None},
                              now=now + i)
    entry = ha._state["codex:host-adapter"]
    assert entry["failures"] == 1
    # One 30-minute window (nudged by the seconds between the failures), not 2 h.
    assert ha.BACKOFF_BASE_S <= entry["retry_after"] - now < 2 * ha.BACKOFF_BASE_S


def test_clear_ends_the_cooldown_and_the_backoff():
    ha.record_unavailable("antigravity:host-adapter", {"reason": "quota", "stated_reset": None})
    assert ha.cooldown("antigravity:host-adapter") is not None
    ha.clear("antigravity:host-adapter")
    assert ha.cooldown("antigravity:host-adapter") is None
    assert "antigravity:host-adapter" not in ha._state
