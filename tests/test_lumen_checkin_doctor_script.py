"""Decision-tree and safety-rail tests for scripts/ops/lumen_checkin_doctor.py.

The doctor's value is the classification being exactly the field runbooks and
the rails (verify-after, flap-cap, operator-only classes) holding. All I/O is
stubbed via the Doctor.io table; classify() is pure.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def doc_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMEN_DOCTOR_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "nosecrets.env"))
    module_path = (
        Path(__file__).resolve().parent.parent
        / "scripts" / "ops" / "lumen_checkin_doctor.py"
    )
    spec = importlib.util.spec_from_file_location("lumen_checkin_doctor", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["lumen_checkin_doctor"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("lumen_checkin_doctor", None)


NOW = 1_800_000_000.0
LUMEN = "69a1a4f7-a30f-4f4a-bcf9-2de8606fb819"


def iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def signals(doc_mod, **kw):
    base = dict(
        central={"status": "active", "last_update": iso(NOW - 60)},
        now=NOW,
        pi_diag={"governance": {"last_decision_source": "unitares_ex"}},
    )
    base.update(kw)
    return doc_mod.Signals(**base)


# ------------------------------------------------------------------ classify

def test_fresh_is_healthy(doc_mod):
    cls, _ = doc_mod.classify(signals(doc_mod))
    assert cls == doc_mod.HEALTHY


def test_central_unreachable(doc_mod):
    cls, _ = doc_mod.classify(doc_mod.Signals(central_error="boom", now=NOW))
    assert cls == doc_mod.UNREACHABLE


def test_pause_near_restart_with_healthy_pi_is_false_pause(doc_mod):
    s = signals(
        doc_mod,
        central={
            "status": "paused",
            "last_update": iso(NOW - 3000),
            "lifecycle_events": [{"event": "paused", "timestamp": iso(NOW - 3000)}],
        },
        gov_start_epoch=NOW - 3100,  # paused 100s after server start
    )
    cls, _ = doc_mod.classify(s)
    assert cls == doc_mod.C1_FALSE_PAUSE


def test_pause_without_restart_proximity_is_real_never_healed(doc_mod):
    s = signals(
        doc_mod,
        central={
            "status": "paused",
            "last_update": iso(NOW - 3000),
            "lifecycle_events": [{"event": "paused", "timestamp": iso(NOW - 3000)}],
        },
        gov_start_epoch=NOW - 86400,  # server up a day — no restart fingerprint
    )
    cls, _ = doc_mod.classify(s)
    assert cls == doc_mod.PAUSED_REAL
    assert cls not in doc_mod.AUTO_HEALABLE


def test_pause_with_unreachable_pi_is_real(doc_mod):
    # Pi health is part of the C1 gate: can't confirm the Pi is fine -> real.
    s = signals(
        doc_mod,
        central={
            "status": "paused",
            "lifecycle_events": [{"event": "paused", "timestamp": iso(NOW - 100)}],
        },
        gov_start_epoch=NOW - 150,
        pi_diag={},
    )
    cls, _ = doc_mod.classify(s)
    assert cls == doc_mod.PAUSED_REAL


def test_frozen_with_dns_journal_is_dns_freeze(doc_mod):
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        journal="Jul 26 anima[123]: Cannot connect to host unitares.tail76aee6.ts.net:8767"
                " [Name or service not known]",
    )
    cls, evidence = doc_mod.classify(s)
    assert cls == doc_mod.C2_DNS_FREEZE
    assert "Cannot connect" in evidence


def test_frozen_with_unknown_tool_journal_wins_over_dns(doc_mod):
    # C3 is checked first: an Unknown-tool rejection means DNS is fine even if
    # older connect errors linger in the 45-min window.
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        journal="anima: Cannot connect to host x\n"
                "anima: UNITARES rejected check-in: Unknown tool: process_agent_update",
    )
    cls, evidence = doc_mod.classify(s)
    assert cls == doc_mod.C3_UNKNOWN_TOOL
    assert "process_agent_update" in evidence


def test_frozen_binding_loss_requires_all_three_signals(doc_mod):
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    full = signals(doc_mod, central=frozen, path2_miss=True, session_rows=0,
                   redis_uptime_s=1000)
    assert doc_mod.classify(full)[0] == doc_mod.C4_BINDING_LOSS
    # Live session row -> PATH2 self-heals; not C4.
    with_row = signals(doc_mod, central=frozen, path2_miss=True, session_rows=1,
                       redis_uptime_s=1000)
    assert doc_mod.classify(with_row)[0] == doc_mod.UNKNOWN
    # Redis older than the freeze -> wipe can't explain it.
    old_redis = signals(doc_mod, central=frozen, path2_miss=True, session_rows=0,
                        redis_uptime_s=999999)
    assert doc_mod.classify(old_redis)[0] == doc_mod.UNKNOWN


def test_frozen_with_recent_restart_is_restart_gap_even_over_dns(doc_mod):
    # Restart-gap takes precedence over the journal classes: boot noise can
    # match the C2 fingerprint and C2's heal is ANOTHER restart (loop risk).
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        journal="anima: Cannot connect to host x [Name or service not known]",
        anima_uptime_s=400.0,
    )
    cls, evidence = doc_mod.classify(s)
    assert cls == doc_mod.RESTART_GAP
    assert cls not in doc_mod.AUTO_HEALABLE
    assert "NOT healing" in evidence


def test_frozen_with_old_service_still_classifies_dns(doc_mod):
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        journal="anima: Cannot connect to host x [Name or service not known]",
        anima_uptime_s=5000.0,
    )
    assert doc_mod.classify(s)[0] == doc_mod.C2_DNS_FREEZE


def test_frozen_right_after_host_wake_is_sleep_gap_even_over_dns_and_restart(doc_mod):
    """The 2026-08-03..05 class: every 'unknown' CRITICAL was the closed lid.

    Central Postgres sleeps with this host, so a freeze that predates the last
    wake says nothing about Lumen — and it must win over the journal classes
    too, because the 45-min journal window is sleep-era noise right after wake
    (a boot-time connect error would otherwise classify C2 and restart the Pi
    services for a freeze the Mac caused).
    """
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        journal="anima: Cannot connect to host x [Name or service not known]",
        anima_uptime_s=400.0,
        host_wake_epoch=NOW - 300,  # awake 5 min, freeze is 66 min old
    )
    cls, evidence = doc_mod.classify(s)
    assert cls == doc_mod.HOST_SLEEP_GAP
    assert cls not in doc_mod.AUTO_HEALABLE
    assert "asleep" in evidence


def test_frozen_past_wake_grace_classifies_normally(doc_mod):
    # Awake longer than the grace: the freeze survived the wake, so it is
    # real evidence again and the ordinary classes take over.
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        journal="anima: Cannot connect to host x [Name or service not known]",
        host_wake_epoch=NOW - 2000,  # awake 33 min > WAKE_GRACE_S
    )
    assert doc_mod.classify(s)[0] == doc_mod.C2_DNS_FREEZE


def test_unknown_wake_time_fails_open_to_real_classes(doc_mod):
    # host_wake_epoch=None (non-macOS, sysctl parse miss) must never swallow
    # a freeze — same fail-open posture as every other missing signal.
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        host_wake_epoch=None,
    )
    assert doc_mod.classify(s)[0] == doc_mod.UNKNOWN


def test_unparseable_central_right_after_wake_is_sleep_gap(doc_mod):
    # A just-woken governance server can answer with an error body during
    # warmup (central={} -> no last_update). Same incident class as the
    # frozen-age variant; escalating critical 'unknown' from that tick would
    # reintroduce the closed-lid alarm through the side door. Once the host
    # has been awake past the grace, an unparseable record is a real anomaly
    # again.
    warmup = signals(doc_mod, central={"status": "active"},
                     host_wake_epoch=NOW - 300)
    assert doc_mod.classify(warmup)[0] == doc_mod.HOST_SLEEP_GAP
    long_awake = signals(doc_mod, central={"status": "active"},
                         host_wake_epoch=NOW - 2000)
    assert doc_mod.classify(long_awake)[0] == doc_mod.UNKNOWN


def test_frozen_no_fingerprint_is_unknown_and_mentions_broker(doc_mod):
    s = signals(doc_mod, central={"status": "active", "last_update": iso(NOW - 4000)})
    cls, evidence = doc_mod.classify(s)
    assert cls == doc_mod.UNKNOWN
    assert "unitares_ex" in evidence


def test_dark_pi_probes_report_blindness_not_unknown(doc_mod):
    """Both Pi SSH reads empty => probe_unreachable, not a Lumen verdict.

    Every fingerprint above the fall-through is keyed off the journal, so an
    empty journal makes UNKNOWN structurally guaranteed regardless of Lumen's
    real state. Reporting that as "no known fingerprint matches" attributes the
    doctor's own blindness to Lumen. This was the standing state from
    2026-08-03 (7/7 criticals, journal_lines=0) while PI_SSH_HOST defaulted to
    the LAN-only `pi-anima` and the operator was mobile.
    """
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        deep=True,
        journal="",
        anima_uptime_s=None,
    )
    cls, evidence = doc_mod.classify(s)
    assert cls == doc_mod.PROBE_UNREACHABLE
    assert "blind" in evidence.lower()


def test_dark_probes_do_not_swallow_a_genuine_unknown(doc_mod):
    """A journal we actually read, with no fingerprint in it, stays UNKNOWN."""
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        deep=True,
        journal="some unrelated broker line\n",
        anima_uptime_s=99999.0,
    )
    cls, evidence = doc_mod.classify(s)
    assert cls == doc_mod.UNKNOWN
    assert "unitares_ex" in evidence


def test_shallow_pass_never_claims_probe_unreachable(doc_mod):
    """Before the deep probes run, empty journal is absence of data, not proof.

    Signals defaults look identical to dark probes; only `deep` separates them.
    """
    s = signals(
        doc_mod,
        central={"status": "active", "last_update": iso(NOW - 4000)},
        journal="",
        anima_uptime_s=None,
    )
    assert doc_mod.classify(s)[0] == doc_mod.UNKNOWN


def test_pi_ssh_host_defaults_to_the_tailscale_alias(tmp_path, monkeypatch):
    """The default must reach the Pi from anywhere, not just the home LAN.

    `pi-anima` resolves through the `Host lumen-local pi-anima` block and times
    out whenever the operator is mobile, which silently blinds every probe here.
    """
    monkeypatch.delenv("LUMEN_SSH_HOST", raising=False)
    monkeypatch.setenv("LUMEN_DOCTOR_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "nosecrets.env"))
    module_path = (
        Path(__file__).resolve().parent.parent
        / "scripts" / "ops" / "lumen_checkin_doctor.py"
    )
    spec = importlib.util.spec_from_file_location("lumen_doctor_defaults", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules[cls.__module__];
    # register before exec, as the doc_mod fixture does.
    sys.modules["lumen_doctor_defaults"] = module
    try:
        spec.loader.exec_module(module)
        assert module.PI_SSH_HOST == "lumen", (
            f"PI_SSH_HOST defaults to {module.PI_SSH_HOST!r}; a LAN-only alias "
            "makes every Pi probe fail while the operator is away"
        )
    finally:
        sys.modules.pop("lumen_doctor_defaults", None)


# ------------------------------------------------------------------ rails

def make_doctor(doc_mod, io_overrides, dry_run=False):
    calls = {"resume": 0, "restart": 0, "findings": []}
    # Advancing fake clock: sleep() moves time forward so verify/wait loops
    # terminate deterministically instead of spinning on a frozen now().
    clock = [NOW]
    io = {
        "now": lambda: clock[0],
        "sleep": lambda s: clock.__setitem__(0, clock[0] + s),
        "central_agent": lambda: {"status": "active", "last_update": iso(NOW - 60)},
        "pi_diagnostics": lambda: {"governance": {"last_decision_source": "unitares_ex"}},
        "gov_process_start_epoch": lambda: NOW - 86400,
        "pi_journal_tail": lambda: "",
        "pi_anima_uptime_s": lambda: 999999.0,
        "live_session_row_count": lambda: 1,
        "redis_uptime_s": lambda: 999999,
        # None = wake time unknowable; rails tests exercise the real classes,
        # and the REAL sysctl on a recently-woken dev Mac must never leak in.
        "host_wake_epoch": lambda: None,
        "path2_miss_recent": lambda: False,
        "resume": lambda token: calls.__setitem__("resume", calls["resume"] + 1)
                   or {"success": True},
        "pi_restart_services": lambda admin: calls.__setitem__(
            "restart", calls["restart"] + 1) or ["tailscaled: ok", "anima: ok"],
        "post_finding": lambda sev, fp, msg, token: calls["findings"].append((sev, fp, msg)),
    }
    io.update(io_overrides)
    return doc_mod.Doctor(io=io, dry_run=dry_run), calls


def test_healthy_run_touches_nothing(doc_mod):
    doctor, calls = make_doctor(doc_mod, {})
    assert doctor.run_once() == doc_mod.HEALTHY
    assert calls["resume"] == 0 and calls["restart"] == 0 and calls["findings"] == []


def test_false_pause_heals_and_verifies(doc_mod):
    state = {"central": {
        "status": "paused",
        "lifecycle_events": [{"event": "paused", "timestamp": iso(NOW - 100)}],
    }}

    def central():
        return state["central"]

    def resume(token):
        state["central"] = {"status": "active", "last_update": iso(NOW - 1)}
        return {"success": True}

    doctor, calls = make_doctor(doc_mod, {
        "central_agent": central,
        "gov_process_start_epoch": lambda: NOW - 150,
        "resume": resume,
    })
    assert doctor.run_once() == doc_mod.C1_FALSE_PAUSE
    # healed + verified -> info finding, not critical
    assert [sev for sev, _, _ in calls["findings"]] == ["info"]


def test_heal_that_fails_verify_escalates_critical(doc_mod):
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    doctor, calls = make_doctor(doc_mod, {
        "central_agent": lambda: frozen,
        "pi_journal_tail": lambda: "Cannot connect to host unitares [Name or service not known]",
    })
    doctor.run_once()
    assert calls["restart"] == 1
    sevs = [sev for sev, _, _ in calls["findings"]]
    assert sevs == ["critical"]
    assert "heal-failed" in calls["findings"][0][1]


def test_flap_cap_stops_healing_and_reports_masking(doc_mod):
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    overrides = {
        "central_agent": lambda: frozen,
        "pi_journal_tail": lambda: "Cannot connect to host unitares [Name or service not known]",
    }
    doctor, calls = make_doctor(doc_mod, overrides)
    doctor.run_once()
    doctor.run_once()
    assert calls["restart"] == 2
    doctor3, calls3 = make_doctor(doc_mod, overrides)  # state persists on disk
    doctor3.run_once()
    assert calls3["restart"] == 0
    assert any("flapping" in fp for _, fp, _ in calls3["findings"])


def test_c4_escalates_with_runbook_and_never_acts(doc_mod):
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    doctor, calls = make_doctor(doc_mod, {
        "central_agent": lambda: frozen,
        "path2_miss_recent": lambda: True,
        "live_session_row_count": lambda: 0,
        "redis_uptime_s": lambda: 1000,
    })
    doctor.run_once()
    assert calls["resume"] == 0 and calls["restart"] == 0
    sev, fp, msg = calls["findings"][0]
    assert sev == "high"
    assert "rebind-resident-session.sh" in msg


def test_alert_cooldown_suppresses_repeat_findings(doc_mod):
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    overrides = {
        "central_agent": lambda: frozen,
        "path2_miss_recent": lambda: True,
        "live_session_row_count": lambda: 0,
        "redis_uptime_s": lambda: 1000,
    }
    doctor, calls = make_doctor(doc_mod, overrides)
    doctor.run_once()
    doctor.run_once()
    assert len(calls["findings"]) == 1


def test_restart_gap_escalates_info_and_never_heals(doc_mod):
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    doctor, calls = make_doctor(doc_mod, {
        "central_agent": lambda: frozen,
        "pi_anima_uptime_s": lambda: 400.0,
        "pi_journal_tail": lambda: "anima: Cannot connect to host x [Name or service not known]",
    })
    assert doctor.run_once() == doc_mod.RESTART_GAP
    assert calls["restart"] == 0 and calls["resume"] == 0
    assert [sev for sev, _, _ in calls["findings"]] == ["info"]


def test_host_sleep_gap_escalates_info_heals_nothing_and_skips_pi_probes(doc_mod):
    """Post-wake tick on a sleep-spanning freeze: info finding, no heal, and
    NO deep probes — SSH to the Pi is exactly what a just-woken network can't
    answer, and the classification needs nothing from it."""
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    probes = {"journal": 0}

    def counting_journal():
        probes["journal"] += 1
        return ""

    doctor, calls = make_doctor(doc_mod, {
        "central_agent": lambda: frozen,
        "host_wake_epoch": lambda: NOW - 300,
        "pi_journal_tail": counting_journal,
    })
    assert doctor.run_once() == doc_mod.HOST_SLEEP_GAP
    assert calls["restart"] == 0 and calls["resume"] == 0
    assert [sev for sev, _, _ in calls["findings"]] == ["info"]
    assert "host_sleep_gap" in calls["findings"][0][1]
    assert probes["journal"] == 0


def test_dry_run_diagnoses_but_never_acts(doc_mod):
    frozen = {"status": "active", "last_update": iso(NOW - 4000)}
    doctor, calls = make_doctor(doc_mod, {
        "central_agent": lambda: frozen,
        "pi_journal_tail": lambda: "Cannot connect to host unitares [Name or service not known]",
    }, dry_run=True)
    assert doctor.run_once() == doc_mod.C2_DNS_FREEZE
    assert calls["restart"] == 0 and calls["findings"] == []
