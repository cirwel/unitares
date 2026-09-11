"""The health watchdog must monitor every surface the port registry declares.

Port 8768 crash-looped for eight days in September 2026 with nothing bound. No
alarm fired, because `scripts/ops/health_watchdog.sh` hand-enumerated two
services (8767, 8766) while `scripts/dev/ports_catalog.py` declared six, and
`deploy_drift_doctor.py`'s surfaces are three git checkouts all bound to
`governance-mcp`. Every instrument reported healthy and every one was telling
the truth about what it actually watched.

`ports_catalog.py` already exists because the *documentation* drifted the same
way — its docstring records the table listing "2 of 5 live ports". The registry
fixed the doc and nobody applied it to the monitoring. These tests close that.

The load-bearing assertion is `test_every_governance_surface_has_a_health_probe`:
adding a service to PORTS without deciding how it is monitored now fails here
rather than in production eight days later.
"""

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
CATALOG = REPO / "scripts" / "dev" / "ports_catalog.py"
WATCHDOG = REPO / "scripts" / "ops" / "health_watchdog.sh"

sys.path.insert(0, str(REPO / "scripts" / "dev"))


def _ports():
    import ports_catalog

    return ports_catalog.PORTS


def test_every_governance_surface_has_a_health_probe():
    """Every governance-host port must declare how it is checked, or say why not.

    `health: None` is a legitimate answer for something genuinely unprobeable —
    but it has to be written down, because the alternative is a surface that
    silently nobody watches.
    """
    undeclared = [
        p["port"] for p in _ports()
        if p.get("host") == "governance host" and "health" not in p
    ]
    assert not undeclared, (
        f"Ports {undeclared} are on the governance host but declare no `health` "
        "block in scripts/dev/ports_catalog.py. Add one (or an explicit "
        "`\"health\": None` with a comment saying why it cannot be probed). "
        "An undeclared surface is an unmonitored surface — this is how 8768 "
        "crash-looped for eight days."
    )


def test_probe_roster_is_not_empty_and_covers_declared_surfaces():
    """The emitter must actually emit. An empty roster reads as 'all clear'."""
    import ports_catalog

    expected = {
        p["port"] for p in _ports()
        if (p.get("health") or {}).get("scope") == "governance"
    }
    emitted = {d["port"] for d in ports_catalog.health_probes("governance")}
    assert emitted, "health_probes() returned nothing — the watchdog would monitor nothing"
    assert emitted == expected, (
        f"Probe roster does not match the registry. Declared {sorted(expected)}, "
        f"emitted {sorted(emitted)}."
    )


def test_probe_roster_cli_matches_the_api():
    """The watchdog shells out, so the CLI path is the one that must work."""
    import ports_catalog

    proc = subprocess.run(
        [sys.executable, str(CATALOG), "--health-probes"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"--health-probes failed: {proc.stderr}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == len(ports_catalog.health_probes("governance"))
    for ln in lines:
        parts = ln.split("\t")
        assert len(parts) == 3, f"expected 3 tab-separated fields, got {len(parts)}: {ln!r}"
        name, url, accepted = parts
        assert name and url.startswith("http://127.0.0.1:")
        assert all(c.strip().isdigit() for c in accepted.split(","))


def test_watchdog_reads_the_registry_instead_of_its_own_list():
    """The whole point: no second roster to forget to update.

    A bare `8767`-style literal creeping back into the probe path is the
    regression — the script may still reference a port in its fallback and in
    the database-detail read, but it must source its roster from the catalog.
    """
    text = WATCHDOG.read_text(encoding="utf-8")
    assert "--health-probes" in text, (
        "health_watchdog.sh no longer reads the roster from ports_catalog.py. "
        "A hand-kept probe list is the defect this test exists to prevent."
    )
    # Ports named outside the documented fallback/db lines would mean a second
    # roster has grown back.
    stray = [
        ln.strip() for ln in text.splitlines()
        if re.search(r"87[0-9]{2}", ln)
        and "GOVERNANCE_HEALTH_URL" not in ln
        and not ln.strip().startswith("#")
    ]
    assert not stray, (
        "These lines hardcode a port outside the governance fallback:\n  "
        + "\n  ".join(stray)
    )


def test_watchdog_treats_an_empty_roster_as_failure():
    """An unavailable registry must page, not pass.

    If the emitter returns nothing the script has monitored nothing, and the
    one thing it must not do is exit quietly — that is indistinguishable from
    every service being healthy.
    """
    text = WATCHDOG.read_text(encoding="utf-8")
    assert "roster" in text and "FAIL health-watchdog roster" in text, (
        "health_watchdog.sh must log a FAIL when the probe roster is empty; "
        "silently checking nothing reads as all-clear."
    )


@pytest.mark.parametrize("port,accepted", [(8789, [200, 401])])
def test_bearer_gated_surfaces_accept_their_gate_response(port, accepted):
    """Reachable is not the same as 200.

    The agent orchestrator is bearer-gated and answers 401 to an
    unauthenticated probe. Demanding 200 would page every five minutes against
    a perfectly healthy service, and an alarm that always fires is an alarm
    nobody reads.
    """
    entry = next((p for p in _ports() if p["port"] == port), None)
    assert entry is not None, f"port {port} vanished from the registry"
    assert entry["health"]["expect"] == accepted, (
        f"port {port} should accept {accepted}; a 200-only check false-alarms."
    )
