"""The operator's ordered reviewer host list (design: dialectic-reviewer-hosts-v0,
step 1). Pure parsing: what a setting means, and which settings are refused."""

from agents.dialectic_reviewer.host_list import MAX_REVIEWER_HOSTS, reviewer_host_plan

def _plan(**env):
    return reviewer_host_plan(env)


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


def test_an_external_host_at_a_local_address_cannot_be_listed():
    # Every spelling of this machine, and a trusted-network address, can be the
    # local floor under another name (codex review of #2652: 127.0.0.1 passed a
    # string comparison against localhost).
    for url in (
        "HTTP://localhost:11434/v1/",
        "http://127.0.0.1:11434/v1",
        "http://127.8.9.10:8000/v1",
        "http://[::1]:11434/v1",
        "http://0.0.0.0:11434/v1",
        "http://192.168.1.20:8000/v1",
        "http://host.docker.internal:11434/v1",
    ):
        plan = _plan(
            UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,external",
            UNITARES_DIALECTIC_EXTERNAL_BASE_URL=url,
        )
        assert plan.hosts == () and "local endpoint" in (plan.error or ""), url


def test_numeric_and_resolved_spellings_of_a_local_address_are_refused(monkeypatch):
    # Codex review of #2652, round 2: shorthand the resolver accepts reached
    # 127.0.0.1 while the URL parser saw a hostname.
    import socket

    real = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "looks-public.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        if host == "really-public.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    for url in (
        "http://127.1:11434/v1",
        "http://2130706433:11434/v1",
        "http://0x7f000001:11434/v1",
        "http://0177.0.0.1:11434/v1",
        "http://looks-public.example:11434/v1",
    ):
        plan = _plan(
            UNITARES_DIALECTIC_REVIEWER_HOSTS="external",
            UNITARES_DIALECTIC_EXTERNAL_BASE_URL=url,
        )
        assert plan.hosts == () and "local endpoint" in (plan.error or ""), url
    plan = _plan(
        UNITARES_DIALECTIC_REVIEWER_HOSTS="external",
        UNITARES_DIALECTIC_EXTERNAL_BASE_URL="https://really-public.example/v1",
    )
    assert plan.keys == ["external"]


def test_an_external_host_at_a_public_address_can_be_listed(monkeypatch):
    import socket

    def unresolvable(*args, **kwargs):
        raise socket.gaierror("offline")

    # A failed lookup adds nothing: the host is judged by its URL alone.
    monkeypatch.setattr(socket, "getaddrinfo", unresolvable)
    plan = _plan(
        UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,external",
        UNITARES_DIALECTIC_EXTERNAL_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    assert plan.keys == ["codex", "external"]


def test_the_privacy_override_cannot_unlock_a_local_external_host(monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_PRIVACY", "external")
    plan = _plan(
        UNITARES_DIALECTIC_REVIEWER_HOSTS="external",
        UNITARES_DIALECTIC_EXTERNAL_BASE_URL="http://127.0.0.1:11434/v1",
    )
    assert plan.hosts == ()


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


def test_the_configured_local_model_cannot_be_listed_even_on_a_public_address(monkeypatch):
    # Codex review of #2652, round 3: an operator's model server on a public
    # address (UNITARES_MODEL_PRIVACY=local) passed the local-address check.
    import socket

    def resolve(host, *args, **kwargs):
        if host in ("models.example.org", "alias.example.org"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 0))]
        raise socket.gaierror("offline")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    floor = "https://models.example.org/v1"
    for external in (floor, "https://alias.example.org/v1", "https://203.0.113.7:443/v1"):
        plan = reviewer_host_plan(
            {
                "UNITARES_DIALECTIC_REVIEWER_HOSTS": "external",
                "UNITARES_DIALECTIC_EXTERNAL_BASE_URL": external,
            },
            local_base_url=floor,
        )
        assert plan.hosts == () and "local model endpoint" in (plan.error or ""), external
    # Another port on the same address is another server.
    plan = reviewer_host_plan(
        {
            "UNITARES_DIALECTIC_REVIEWER_HOSTS": "external",
            "UNITARES_DIALECTIC_EXTERNAL_BASE_URL": "https://203.0.113.7:8443/v1",
        },
        local_base_url=floor,
    )
    assert plan.keys == ["external"]


def test_a_malformed_external_url_is_an_invalid_list_not_a_crash():
    plan = _plan(
        UNITARES_DIALECTIC_REVIEWER_HOSTS="codex,external",
        UNITARES_DIALECTIC_EXTERNAL_BASE_URL="http://[::1/v1",
    )
    assert plan.hosts == () and "does not parse" in (plan.error or "")


def test_a_refused_url_is_named_without_its_credentials(monkeypatch):
    # Codex review of #2652, round 6: the error is persisted as provenance, so
    # it must not quote userinfo, path or query.
    import socket

    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    secret = "SECRET-must-not-surface"
    for external, local in (
        (f"http://user:{secret}@127.0.0.1:11434/v1?api_key={secret}", None),
        (f"https://models.example.org/{secret}/v1?key={secret}", "https://models.example.org/v1"),
        (f"http://[::1/v1?api_key={secret}", None),
    ):
        plan = reviewer_host_plan(
            {
                "UNITARES_DIALECTIC_REVIEWER_HOSTS": "external",
                "UNITARES_DIALECTIC_EXTERNAL_BASE_URL": external,
            },
            local_base_url=local or "http://localhost:11434/v1",
        )
        assert plan.hosts == () and plan.error, external
        assert secret not in plan.error and "user" not in plan.error, plan.error
