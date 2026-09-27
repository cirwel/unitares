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



def test_a_stated_reset_overrides_a_running_guess_even_when_sooner():
    """Review of #2486 (antigravity, round 3): a guessed backoff must not hide
    the provider's own, earlier reset time."""
    now = 1_000_000.0
    ha.record_unavailable("claude:host-adapter", {"reason": "auth", "stated_reset": None}, now=now)
    ha.record_unavailable("claude:host-adapter",
                          {"reason": "quota", "stated_reset": now + 300}, now=now + 1)
    entry = ha._state["claude:host-adapter"]
    assert entry["retry_after"] == now + 300 and entry["retry_after_source"] == "provider"
    # ...while a second guess inside the window still never shortens it.
    ha.record_unavailable("codex:host-adapter", {"reason": "auth", "stated_reset": None}, now=now)
    first = ha._state["codex:host-adapter"]["retry_after"]
    ha.record_unavailable("codex:host-adapter", {"reason": "auth", "stated_reset": None},
                          now=now - 10)
    assert ha._state["codex:host-adapter"]["retry_after"] == first



def test_a_guess_never_overwrites_a_stated_reset_in_its_window():
    """Review of #2486 (antigravity, round 4): an auth failure with no reset
    time, arriving while a stated reset runs, replaced it with a later guess."""
    now = 1_000_000.0
    ha.record_unavailable("codex:host-adapter",
                          {"reason": "quota", "stated_reset": now + 300}, now=now)
    ha.record_unavailable("codex:host-adapter",
                          {"reason": "auth", "stated_reset": None}, now=now + 1)
    entry = ha._state["codex:host-adapter"]
    assert entry["retry_after"] == now + 300 and entry["retry_after_source"] == "provider"
    # The kept window keeps its cause: the auth guess did not set it.
    assert entry["reason"] == "quota"


# --- Redis copy: a restart remembers a provider at its limit ---------------

fakeredis = pytest.importorskip("fakeredis")
import asyncio  # noqa: E402
import json  # noqa: E402

import fakeredis.aioredis  # noqa: E402

QUOTA = {"reason": "quota", "stated_reset": None}


@pytest.fixture
def redis(monkeypatch):
    """A fake Redis in place of the live one (conftest cuts the live one off)."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _fake():
        return fake

    monkeypatch.setattr(ha, "_get_redis", _fake)
    return fake


def _restart():
    """What a gov restart does to this module: the process state is gone."""
    ha._reset_for_tests()


@pytest.mark.asyncio
async def test_a_cooldown_survives_a_restart(redis):
    now = 1_000_000.0
    view = await ha.record_unavailable_async(
        "codex:host-adapter", {"reason": "quota", "stated_reset": now + 3600},
        detail="usage limit", now=now)
    key = ha.REDIS_KEY_PREFIX + "codex:host-adapter"
    assert 3590 <= await redis.ttl(key) <= 3600  # TTL = retry_after - now

    _restart()
    assert ha.cooldown("codex:host-adapter", now=now + 60) is None  # cache empty
    assert await ha.load_from_redis(now=now + 60) == 1
    restored = ha.cooldown("codex:host-adapter", now=now + 60)
    assert restored == view  # same window, source, reason and failure count
    assert ha.cooldown("codex:host-adapter", now=now + 3601) is None


@pytest.mark.asyncio
async def test_a_restored_window_follows_the_window_rules(redis):
    now = 1_000_000.0
    await ha.record_unavailable_async("claude:host-adapter", QUOTA, now=now)
    _restart()
    await ha.load_from_redis(now=now + 1)
    # Inside the restored window a further guess changes nothing...
    await ha.record_unavailable_async("claude:host-adapter", QUOTA, now=now + 2)
    assert ha._state["claude:host-adapter"]["retry_after"] == now + ha.BACKOFF_BASE_S
    assert ha._state["claude:host-adapter"]["failures"] == 1
    # ...and a provider-stated reset still wins over the restored guess.
    await ha.record_unavailable_async(
        "claude:host-adapter", {"reason": "quota", "stated_reset": now + 120}, now=now + 3)
    _restart()
    await ha.load_from_redis(now=now + 4)
    entry = ha._state["claude:host-adapter"]
    assert entry["retry_after"] == now + 120 and entry["retry_after_source"] == "provider"


@pytest.mark.asyncio
async def test_the_backoff_keeps_counting_across_a_restart(redis):
    """The failure count comes back with the window, so the first failure
    after a restored window lapses backs off one step longer, not from 30m."""
    now = 1_000_000.0
    await ha.record_unavailable_async("claude:host-adapter", QUOTA, now=now)
    _restart()
    await ha.load_from_redis(now=now + 1)
    after = now + ha.BACKOFF_BASE_S + 1  # the restored window has lapsed
    view = await ha.record_unavailable_async("claude:host-adapter", QUOTA, now=after)
    assert view["consecutive_failures"] == 2
    assert ha._state["claude:host-adapter"]["retry_after"] == after + 2 * ha.BACKOFF_BASE_S


@pytest.mark.asyncio
async def test_a_write_syncs_a_window_it_did_not_know_about(redis):
    """Another process (or the run before a restart whose load lost the race
    with the first call) holds a stated reset: a local guess must not replace
    it in Redis."""
    now = 1_000_000.0
    await ha.record_unavailable_async(
        "codex:host-adapter", {"reason": "quota", "stated_reset": now + 7200}, now=now)
    _restart()  # no load: the cache has not seen it
    view = await ha.record_unavailable_async(
        "codex:host-adapter", {"reason": "auth", "stated_reset": None}, now=now + 5)
    assert view["retry_after_source"] == "provider"
    assert view["reason"] == "quota"
    stored = json.loads(await redis.get(ha.REDIS_KEY_PREFIX + "codex:host-adapter"))
    assert stored["retry_after"] == now + 7200


@pytest.mark.asyncio
async def test_a_stored_window_is_still_capped_at_twelve_hours(redis):
    now = 1_000_000.0
    await redis.set(ha.REDIS_KEY_PREFIX + "codex:host-adapter", json.dumps({
        "reason": "quota", "retry_after": now + 7 * 86400, "retry_after_source": "provider",
        "failures": 1, "detail": "", "recorded_at": now}))
    await ha.load_from_redis(now=now)
    assert ha._state["codex:host-adapter"]["retry_after"] == now + ha.STATED_RESET_CAP_S


@pytest.mark.asyncio
async def test_lapsed_and_foreign_copies_are_ignored(redis):
    now = 1_000_000.0
    await redis.set(ha.REDIS_KEY_PREFIX + "claude:host-adapter", json.dumps({
        "reason": "quota", "retry_after": now - 1, "retry_after_source": "backoff",
        "failures": 1}))
    await redis.set(ha.REDIS_KEY_PREFIX + "codex:host-adapter", "not json")
    await redis.set(ha.REDIS_KEY_PREFIX + "antigravity:host-adapter", json.dumps({
        "retry_after": now + 60, "retry_after_source": "made-up"}))
    assert await ha.load_from_redis(now=now) == 0
    assert ha._state == {}


@pytest.mark.asyncio
async def test_clear_removes_the_copy_so_a_restart_does_not_resurrect_it(redis):
    now = 1_000_000.0
    await ha.record_unavailable_async("claude:host-adapter", QUOTA, now=now)
    await ha.clear_async("claude:host-adapter")
    assert await redis.get(ha.REDIS_KEY_PREFIX + "claude:host-adapter") is None
    _restart()
    assert await ha.load_from_redis(now=now + 1) == 0


class _BrokenRedis:
    async def get(self, *a, **k):
        raise ConnectionError("redis down")

    set = delete = mget = get

    def scan_iter(self, *a, **k):
        raise ConnectionError("redis down")


class _HangingRedis:
    async def get(self, *a, **k):
        await asyncio.sleep(3600)

    set = delete = mget = get

    async def scan_iter(self, *a, **k):
        await asyncio.sleep(3600)
        yield ""


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [None, _BrokenRedis(), _HangingRedis()],
                         ids=["absent", "raising", "hanging"])
async def test_redis_down_means_in_process_behaviour(monkeypatch, client):
    async def _redis():
        return client

    monkeypatch.setattr(ha, "_get_redis", _redis)
    monkeypatch.setattr(ha, "REDIS_TIMEOUT_S", 0.05)
    now = 1_000_000.0
    view = await ha.record_unavailable_async(
        "codex:host-adapter", {"reason": "quota", "stated_reset": now + 600}, now=now)
    assert view["retry_after_source"] == "provider"
    assert ha.cooldown("codex:host-adapter", now=now + 1) is not None
    assert await ha.load_from_redis(now=now + 1) == 0
    assert ha.cooldown("codex:host-adapter", now=now + 1) is not None  # load kept it
    await ha.clear_async("codex:host-adapter")
    assert ha.cooldown("codex:host-adapter", now=now + 1) is None


@pytest.mark.asyncio
async def test_a_failed_delete_on_recovery_is_logged(monkeypatch, caplog):
    async def _redis():
        return _BrokenRedis()

    monkeypatch.setattr(ha, "_get_redis", _redis)
    await ha.clear_async("codex:host-adapter")
    assert "was not deleted" in caplog.text


def test_an_older_stated_reset_arriving_late_does_not_replace_a_newer_one():
    """Review round 3 (antigravity): the call that saw its failure first can
    finish second; the later statement must still win."""
    now = 1_000_000.0
    ha.record_unavailable("codex:host-adapter",
                          {"reason": "quota", "stated_reset": now + 600}, now=now + 10)
    ha.record_unavailable("codex:host-adapter",
                          {"reason": "quota", "stated_reset": now + 3600}, now=now + 5)
    entry = ha._state["codex:host-adapter"]
    assert entry["retry_after"] == now + 600 and entry["recorded_at"] == now + 10


@pytest.mark.asyncio
async def test_concurrent_stated_resets_keep_the_later_statement(monkeypatch):
    """The same race end to end: the first caller's Redis read is slow."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    slow_once = {"left": 1}

    class _SlowFirstRead:
        def __getattr__(self, name):
            return getattr(fake, name)

        async def get(self, key):
            if slow_once["left"]:
                slow_once["left"] -= 1
                await asyncio.sleep(0.05)
            return await fake.get(key)

    async def _redis():
        return _SlowFirstRead()

    monkeypatch.setattr(ha, "_get_redis", _redis)
    now = 1_000_000.0
    first = ha.record_unavailable_async(
        "codex:host-adapter", {"reason": "quota", "stated_reset": now + 3600}, now=now)
    second = ha.record_unavailable_async(
        "codex:host-adapter", {"reason": "quota", "stated_reset": now + 600}, now=now + 1)
    await asyncio.gather(first, second)
    assert ha._state["codex:host-adapter"]["retry_after"] == now + 600
    stored = json.loads(await fake.get(ha.REDIS_KEY_PREFIX + "codex:host-adapter"))
    assert stored["retry_after"] == now + 600
