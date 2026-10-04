"""The operator's ordered reviewer host list (design: dialectic-reviewer-hosts-v0,
step 1). Pure parsing: what a setting means, and which settings are refused."""

from agents.dialectic_reviewer.host_list import MAX_REVIEWER_HOSTS, reviewer_host_plan

LOCAL = "http://localhost:11434/v1"


def _plan(**env):
    return reviewer_host_plan(env, local_base_url=LOCAL)


def test_no_setting_means_no_list_and_the_local_model_reviews():
    plan = _plan()
    assert plan.hosts == () and plan.listed is False and plan.error is None


def test_the_legacy_single_host_is_a_one_item_list():
    plan = _plan(UNITARES_DIALECTIC_REVIEWER_HOST="codex")
    assert plan.keys == ["codex"] and plan.listed is True


def test_a_legacy_floor_name_still_means_no_list():
    for name in ("local", "ollama", "ollama:local", " "):
        plan = _plan(UNITARES_DIALECTIC_REVIEWER_HOST=name)
        assert plan.listed is False and plan.error is None, name


def test_the_list_keeps_the_operators_order_and_outranks_the_legacy_name():
    plan = _plan(
        UNITARES_DIALECTIC_REVIEWER_HOSTS=" antigravity, codex:host-adapter ,claude",
        UNITARES_DIALECTIC_REVIEWER_HOST="gemini",
    )
    assert plan.keys == ["antigravity", "codex", "claude"]


def test_an_invalid_list_names_its_reason_and_calls_no_host():
    cases = {
        "codex,ollama": "local floor",
        "local": "local floor",
        "codex,mystery": "unknown host",
        "codex,codex:host-adapter": "listed twice",
        "codex,claude,antigravity,external": f"at most {MAX_REVIEWER_HOSTS}",
        ",": "no host named",
    }
    for raw, reason in cases.items():
        plan = _plan(UNITARES_DIALECTIC_REVIEWER_HOSTS=raw)
        assert plan.hosts == () and plan.listed is True, raw
        assert reason in (plan.error or ""), (raw, plan.error)


def test_an_unknown_legacy_host_is_invalid_not_ignored():
    # Before the list, an unknown name fell back to the local model with its
    # approval withheld; an invalid plan keeps that outcome.
    plan = _plan(UNITARES_DIALECTIC_REVIEWER_HOST="grok")
    assert plan.listed is True and "unknown host" in plan.error


def test_an_external_host_that_is_the_local_endpoint_cannot_be_listed():
    plan = _plan(
        UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,external",
        UNITARES_DIALECTIC_EXTERNAL_BASE_URL="HTTP://localhost:11434/v1/",
    )
    assert plan.hosts == () and "local endpoint" in plan.error


def test_only_the_cli_hosts_and_the_configured_external_host_may_approve():
    plan = _plan(UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,antigravity,external")
    assert all(host.may_approve for host in plan.hosts)


def test_the_digest_moves_with_the_list_and_the_external_model():
    base = _plan(UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,external",
                 UNITARES_DIALECTIC_EXTERNAL_MODEL="m1")
    assert base.digest == _plan(UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,external",
                                UNITARES_DIALECTIC_EXTERNAL_MODEL="m1").digest
    assert base.digest != _plan(UNITARES_DIALECTIC_REVIEWER_HOSTS="external,codex",
                                UNITARES_DIALECTIC_EXTERNAL_MODEL="m1").digest
    assert base.digest != _plan(UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,external",
                                UNITARES_DIALECTIC_EXTERNAL_MODEL="m2").digest
