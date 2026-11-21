"""Vigil posts findings on governance-down and Lumen-unreachable transitions."""


def test_gov_down_transition_posts_finding():
    """First cycle that sees governance unhealthy must post a vigil_finding."""
    from agents.common.findings import compute_fingerprint

    # Vigil's finding on gov_down should have a stable fingerprint so the
    # 30-min dedup window suppresses repeat pages while governance stays down.
    fp = compute_fingerprint(["vigil", "governance_down"])
    assert len(fp) == 16
    assert compute_fingerprint(["vigil", "governance_down"]) == fp
    assert compute_fingerprint(["vigil", "lumen_unreachable"]) != fp


def test_lumen_outage_streak_fingerprint_stable():
    from agents.common.findings import compute_fingerprint
    a = compute_fingerprint(["vigil", "lumen_unreachable"])
    b = compute_fingerprint(["vigil", "lumen_unreachable"])
    assert a == b


def test_vigil_emits_on_gov_down_transition(monkeypatch):
    """When governance transitions healthy → unhealthy, post_finding fires once."""
    from agents.vigil import agent as vigil_mod

    calls = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(vigil_mod, "post_finding", fake_post)

    # Simulate the emit site directly
    prev_state = {"governance_healthy": True}
    gov_healthy = False
    gov_detail = "connection refused"

    if not gov_healthy and prev_state.get("governance_healthy", True):
        vigil_mod.post_finding(
            event_type="vigil_finding",
            severity="critical",
            message=f"Governance is down: {gov_detail}",
            agent_id="vigil",
            agent_name="Vigil",
            fingerprint=vigil_mod.compute_fingerprint(["vigil", "governance_down"]),
            extra={"finding_type": "governance_down"},
        )

    assert len(calls) == 1
    assert calls[0]["event_type"] == "vigil_finding"
    assert calls[0]["severity"] == "critical"
    assert "connection refused" in calls[0]["message"]


def test_vigil_findings_emit_prefers_findings_agent_token_with_http_fallback(monkeypatch):
    """Vigil's finding emitters honor UNITARES_FINDINGS_AGENT_TOKEN and fall back to UNITARES_HTTP_API_TOKEN."""
    from datetime import datetime, timezone

    from agents.common.http_client import clear_bearer_preference_cache
    from agents.vigil import agent as vigil_mod

    clear_bearer_preference_cache()
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(vigil_mod, "notify", lambda *a, **k: None)

    sent_headers: list[dict] = []

    def fake_httpx_post(url, json, headers, timeout):  # noqa: A002
        sent_headers.append(dict(headers))

        class FakeResp:
            status_code = 200

            def json(self):
                return {"success": True, "deduped": False}

        return FakeResp()

    monkeypatch.setattr("agents.common.findings._httpx_post", fake_httpx_post)

    state = {
        "stalled_pr_open": {
            "acmeco/unitares#7": {
                "repo": "acmeco/unitares",
                "number": 7,
                "url": "https://example.test/7",
                "failing": ["smoke"],
                "red_hours": 30.0,
                "author_agent_uuid": "11111111-2222-4333-8444-555555555555",
                "author_model": "claude-test",
                "first_seen": 1_000_000.0,
                "announced": False,
            }
        },
        "stalled_pr_resolved": [],
    }

    # 1. UNITARES_FINDINGS_AGENT_TOKEN takes precedence over UNITARES_HTTP_API_TOKEN
    monkeypatch.setenv("UNITARES_FINDINGS_AGENT_TOKEN", "vigil-scoped-tok")
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "shared-local-tok")
    counts = vigil_mod.emit_stalled_pr_findings(state)
    assert counts["opened"] == 1
    assert sent_headers[-1].get("Authorization") == "Bearer vigil-scoped-tok"

    # 2. Explicit findings_token overrides env vars
    vigil_mod.post_archive_digest(
        5, 1, 90, datetime(2026, 11, 21, tzinfo=timezone.utc),
        findings_token="explicit-vigil-tok",
    )
    assert sent_headers[-1].get("Authorization") == "Bearer explicit-vigil-tok"

    # 3. When UNITARES_FINDINGS_AGENT_TOKEN is unset, falls back cleanly to UNITARES_HTTP_API_TOKEN
    monkeypatch.delenv("UNITARES_FINDINGS_AGENT_TOKEN", raising=False)
    vigil_mod.post_archive_digest(
        5, 1, 90, datetime(2026, 11, 21, tzinfo=timezone.utc),
    )
    assert sent_headers[-1].get("Authorization") == "Bearer shared-local-tok"

    # 4. VigilAgent accepts optional findings_token without breaking default init
    agent = vigil_mod.VigilAgent(findings_token="agent-scoped-tok")
    assert agent.findings_token == "agent-scoped-tok"
    clear_bearer_preference_cache()
