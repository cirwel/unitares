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

import importlib.util
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
CATALOG = REPO / "scripts" / "dev" / "ports_catalog.py"
WATCHDOG = REPO / "scripts" / "ops" / "health_watchdog.sh"


def _catalog():
    """Load ports_catalog by path, without touching sys.path.

    `scripts/dev/` holds 60+ generically-named modules (version_manager,
    flag_catalog, glossary_data). Inserting it on sys.path at module scope
    would shadow any future same-named module in src/ for the rest of the
    pytest session, surfacing as a failure in an unrelated test file.
    """
    spec = importlib.util.spec_from_file_location("_ports_catalog_under_test", CATALOG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ports():
    return _catalog().PORTS


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
    malformed = []
    for p in _ports():
        h = p.get("health")
        if h is None:
            continue
        if not isinstance(h, dict) or not h.get("path") or not h.get("expect"):
            malformed.append(p["port"])
    assert not malformed, (
        f"Ports {malformed} have a `health` block missing `path` or `expect`. "
        "health_probes() indexes both directly, so one malformed entry raises "
        "KeyError and collapses the roster for EVERY service at once."
    )

    assert not undeclared, (
        f"Ports {undeclared} are on the governance host but declare no `health` "
        "block in scripts/dev/ports_catalog.py. Add one (or an explicit "
        "`\"health\": None` with a comment saying why it cannot be probed). "
        "An undeclared surface is an unmonitored surface — this is how 8768 "
        "crash-looped for eight days."
    )


def test_probe_roster_is_not_empty_and_covers_declared_surfaces():
    """The emitter must actually emit. An empty roster reads as 'all clear'."""
    ports_catalog = _catalog()

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
    ports_catalog = _catalog()

    proc = subprocess.run(
        [sys.executable, str(CATALOG), "--health-probes"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"--health-probes failed: {proc.stderr}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == len(ports_catalog.health_probes("governance"))
    for ln in lines:
        parts = ln.split("\t")
        assert len(parts) == 4, f"expected 4 tab-separated fields, got {len(parts)}: {ln!r}"
        name, url, accepted, role = parts
        assert name and url.startswith("http://127.0.0.1:")
        assert all(c.strip().isdigit() for c in accepted.split(","))


# The three probes the script is allowed to name directly. Everything else must
# come from the registry. This IS a hand-maintained list, deliberately: a roster
# in the script fails OPEN (a forgotten service is silently unmonitored), while
# a roster in the test fails CLOSED (the build breaks and someone decides). The
# asymmetry is the whole argument for the coverage test above.
EXPECTED_CHECK_SITES = {
    "$name",                   # the roster loop, one call for every registry row
    "governance (fallback)",   # degraded path, when the registry is unreadable
    "anima",                   # edge node on another host — see the script
}


def test_watchdog_reads_the_registry_instead_of_its_own_list():
    """The whole point: no second roster to forget to update.

    Asserted as the SET of probe names, not their count. A count is a lossy
    hash of the list and is blind to substitution — deleting the anima probe
    and adding a hand-kept one in its place keeps the total at three, which is
    exactly the regression this guard was written after. Matching anywhere on
    the line rather than at its start also catches ``if ! check "..."`` and a
    call that follows an assignment.
    """
    text = WATCHDOG.read_text(encoding="utf-8")
    assert "--health-probes" in text, (
        "health_watchdog.sh no longer reads its roster from ports_catalog.py. "
        "A hand-kept probe list is the defect this test exists to prevent."
    )

    found = {
        m.group(1)
        for ln in text.splitlines()
        if not ln.strip().startswith("#")
        for m in [re.search(r'\bcheck\s+"([^"]*)"', ln)]
        if m
    }
    added = found - EXPECTED_CHECK_SITES
    removed = EXPECTED_CHECK_SITES - found
    assert not added and not removed, (
        "The set of direct `check` call sites changed.\n"
        f"  added:   {sorted(added) or 'none'}\n"
        f"  removed: {sorted(removed) or 'none'}\n"
        "An addition means a hand-kept probe grew back instead of being "
        "declared in ports_catalog.py. A removal means a probe was dropped — "
        "the anima one has already been lost this way once. If the change is "
        "intended, update EXPECTED_CHECK_SITES deliberately."
    )


def test_exactly_one_registry_entry_claims_the_governance_role():
    """The watchdog finds the governance probe by role, so the role must be unique.

    Keying off `:8767/` in the URL instead would re-create the coupling this
    registry removes, and fail silently: if the governance port moved, the
    Postgres-pool read would simply never run and the script would exit 0
    having written nothing.
    """
    governance = [
        p["port"] for p in _ports()
        if (p.get("health") or {}).get("role") == "governance"
    ]
    assert len(governance) == 1, (
        f"Expected exactly one entry with role='governance', found {governance}. "
        "Zero means health_watchdog.sh cannot locate the governance probe and "
        "the pool check silently never runs; more than one makes which URL it "
        "uses depend on registry order."
    )


def test_catalog_compiles_under_the_system_interpreter():
    """The watchdog shells out via bare `python3`, which launchd resolves to /usr/bin.

    pyproject requires >=3.12, but `scripts/ops/health_watchdog.sh` runs under
    launchd's minimal PATH where `python3` is the system interpreter — 3.9.6 on
    current macOS. This module therefore has a lower floor than the rest of the
    repo, and nothing else enforces it. A 3.12-only construct added here (an
    f-string containing a backslash, say) would not fail any other test; it
    would collapse the watchdog's roster to the fallback branch on every run,
    dropping six monitored services to one.

    Skipped where no system interpreter exists, since the constraint only binds
    on hosts that have one.
    """
    import py_compile

    system_python = pathlib.Path("/usr/bin/python3")
    if not system_python.exists():
        pytest.skip("no /usr/bin/python3 on this host")

    proc = subprocess.run(
        [str(system_python), "-m", "py_compile", str(CATALOG)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"{CATALOG.name} does not compile under {system_python} — the "
        "interpreter health_watchdog.sh actually gets under launchd:\n"
        f"{proc.stderr}\nKeep this module compatible with the oldest python3 a "
        "deployment might resolve, or pin the interpreter in the script."
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
