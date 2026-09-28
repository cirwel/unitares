"""What a fresh install does when it has no residents.

This is the configuration EVERY adopter gets — `UNITARES_RESIDENTS` unset —
and until 2026-08-18 it was the least-exercised path in the repo.
`tests/conftest.py` declares this operator's six residents for the whole
suite, so ~12,290 of 12,294 tests run in a fleet-present world and two
parser unit tests covered the fleet-absent one. Running the suite with an
empty roster produced 49 failures, nearly all of them tests whose *premise*
(these residents exist) is void rather than product defects — which is worse
in one specific way: it means there was an implementation of the residentless
install but no specification of it.

That gap is how the `/v1/residents` tier-3 bug survived. It filtered the
declared roster through a hardcoded list of six names, so an adopter's roster
resolved to an EMPTY list still reported as `source: "known-residents"`. With
the roster populated, that filter is a no-op and nothing can see it.

So this module asserts what SHOULD happen with no residents, at the level
that matters — resolvers, classifiers, and defaults, not just the parser. It
patches the import-time module constants rather than the environment, because
`KNOWN_RESIDENT_LABELS` / `KNOWN_RESIDENT_ORDER` are read once at import.

The invariant under test is the one in the shared contract: **a resident name
may be read from config and displayed; it may never be branched on.** With no
config, no name means anything.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.grounding.class_indicator as ci
import src.grounding.onboard_classifier as oc
from src.grounding.class_indicator import (
    classify_by_label_and_tags,
    load_resident_labels,
    parse_resident_roster,
    parse_resident_roster_order,
)
from src.http_routes.residents import (
    _load_resident_silence_seconds,
    _resolve_resident_labels,
)
from src.resident_progress.probe_task import ProgressFlatProbe


@pytest.fixture
def residentless(monkeypatch):
    """A deployment that declared no residents — the shipped default."""
    monkeypatch.setattr(ci, "KNOWN_RESIDENT_LABELS", frozenset())
    monkeypatch.setattr(ci, "KNOWN_RESIDENT_ORDER", ())
    monkeypatch.setattr(oc, "KNOWN_RESIDENT_LABELS", frozenset())
    monkeypatch.delenv("UNITARES_RESIDENTS", raising=False)
    monkeypatch.delenv("UNITARES_RESIDENT_AGENTS", raising=False)
    monkeypatch.delenv("UNITARES_RESIDENT_SILENCE_SECONDS", raising=False)


def _meta(label=None, resident=False, tags=None):
    return SimpleNamespace(
        label=label, display_name=label, resident=resident, tags=tags or []
    )


class TestTheRosterIsEmpty:
    def test_unset_env_yields_no_residents(self, residentless):
        assert load_resident_labels() == frozenset()
        assert parse_resident_roster(None) == frozenset()
        assert parse_resident_roster_order(None) == ()
        assert parse_resident_roster_order("") == ()

    def test_declared_order_is_preserved_and_deduped(self):
        # Order is config, so it must survive verbatim — this is what the
        # dashboard renders.
        assert parse_resident_roster_order("Beta, Alpha ,Beta, Gamma") == (
            "Beta",
            "Alpha",
            "Gamma",
        )

    def test_set_and_order_agree_on_membership(self):
        raw = "Kestrel,Aurora,Tern"
        assert set(parse_resident_roster_order(raw)) == parse_resident_roster(raw)


class TestNoNameIsSpecial:
    def test_a_famous_name_is_just_a_string(self, residentless):
        # The single clearest statement of the contract: with no roster, the
        # maintainer's own embodied agent is an ordinary ephemeral agent.
        assert oc.default_tags_for_onboard("Lumen", existing_tags=[]) == list(
            oc.EPHEMERAL_DEFAULT_TAGS
        )

    def test_classification_falls_through_to_tags(self, residentless):
        # Not its own N=1 class — classified by what it CAN DO, which is the
        # generic discriminator.
        assert classify_by_label_and_tags("Lumen", ["embodied"], known=frozenset()) == (
            ci.CLASS_EMBODIED
        )
        assert classify_by_label_and_tags(
            "Vigil", ["persistent", "autonomous"], known=frozenset()
        ) == ci.CLASS_RESIDENT_PERSISTENT

    def test_untagged_agent_lands_on_a_real_default_not_an_error(self, residentless):
        cls = classify_by_label_and_tags("Anything", [], known=frozenset())
        assert cls in {ci.CLASS_EPHEMERAL, ci.CLASS_DEFAULT}


class TestResolverReportsAbsenceHonestly:
    def test_empty_fleet_resolves_to_none_not_a_false_success(self, residentless):
        server = SimpleNamespace(agent_metadata={})
        labels, source = _resolve_resident_labels(server)
        assert labels == []
        assert source == "none"

    def test_agents_present_but_no_roster_still_resolves_to_none(self, residentless):
        # Names alone confer nothing. This is the honest-absence contract, not
        # a regression test for the tier-3 bug: that bug needed a NON-empty
        # roster of non-matching names, and is covered by
        # test_http_residents_resolve.py::
        # test_roster_of_names_this_operator_does_not_use_survives. Here the
        # point is that an empty answer must be LABELLED empty, so a caller can
        # tell "this deployment declared nobody" from "the roster resolved".
        server = SimpleNamespace(
            agent_metadata={"a1": _meta("Lumen"), "a2": _meta("Vigil")}
        )
        labels, source = _resolve_resident_labels(server)
        assert labels == []
        assert source == "none"

    def test_route_local_override_still_works_without_a_roster(self, monkeypatch, residentless):
        # UNITARES_RESIDENT_AGENTS is the per-endpoint override and does not
        # depend on the calibration roster — an operator can surface a
        # dashboard list without declaring calibration classes.
        monkeypatch.setenv("UNITARES_RESIDENT_AGENTS", "Kestrel, Aurora")
        labels, source = _resolve_resident_labels(SimpleNamespace(agent_metadata={}))
        assert labels == ["Kestrel", "Aurora"]
        assert source == "env"

    def test_metadata_flag_still_works_without_a_roster(self, residentless):
        # Tier 2 is a capability flag on the agent, not a name, so it must
        # survive an empty roster.
        server = SimpleNamespace(agent_metadata={"a1": _meta("Kestrel", resident=True)})
        labels, source = _resolve_resident_labels(server)
        assert labels == ["Kestrel"]
        assert source == "metadata"


class TestDefaultsCarryNoFleet:
    def test_silence_thresholds_are_empty_by_default(self, residentless):
        # These were five of this operator's residents and their cron cadences
        # as library constants until 2026-08-18.
        assert _load_resident_silence_seconds() == {}

    def test_an_adopter_can_declare_their_own(self, monkeypatch, residentless):
        monkeypatch.setenv("UNITARES_RESIDENT_SILENCE_SECONDS", "kestrel=900,tern=86400")
        assert _load_resident_silence_seconds() == {"kestrel": 900, "tern": 86400}


class TestTheProgressProbeRunsWithNoManifest:
    @pytest.mark.asyncio
    async def test_a_tick_probes_nobody_and_records_only_itself(self, monkeypatch):
        # The progress roster is its own manifest (UNITARES_RESIDENT_PROGRESS_MANIFEST),
        # empty by default. The tick groups residents by (source, window); with
        # no residents there is no group, so nothing is fetched or evaluated and
        # the probe's own row is the whole write.
        monkeypatch.setattr(
            "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY", {}
        )
        source = MagicMock(fetch=AsyncMock())
        heartbeat = MagicMock(evaluate=AsyncMock())
        writer = MagicMock(write=AsyncMock())
        audit = MagicMock(emit=AsyncMock())

        await ProgressFlatProbe(
            sources_by_name={"agent_checkins": source},
            heartbeat_evaluator=heartbeat,
            writer=writer,
            audit_emitter=audit,
        ).tick()

        source.fetch.assert_not_awaited()
        heartbeat.evaluate.assert_not_awaited()
        audit.emit.assert_not_awaited()
        written = [row for awaited in writer.write.await_args_list for row in awaited.args[0]]
        assert [(r.resident_label, r.metric_value) for r in written] == [
            ("progress_flat_probe", 0)
        ]


class TestANamedMintIsRoutine:
    @pytest.mark.asyncio
    async def test_start_session_lifts_the_no_roster_verdict_compact(self, residentless):
        # With no roster every named mint reads no_roster_configured, "the
        # correct outcome for a residentless install". start_session lifts that
        # verdict without its prose and keeps the routine shape.
        from tests.helpers import onboard_producer

        arguments = {"force_new": True, "name": "my-agent"}
        payload = await onboard_producer.mint(arguments)
        env, wire = await onboard_producer.start_session(arguments, payload)

        assert payload["resident_registration"]["status"] == "no_roster_configured"
        assert env["resident_registration"] == {
            "status": "no_roster_configured",
            "on_roster": False,
        }
        assert env["response_shape"] == "routine"
        assert "raw_governance" not in env
        assert "_response_size" not in env
        assert wire <= 1_500  # tests/test_response_budgets.py, named class


class TestTheGuardCoversWhatShips:
    def test_guard_scope_tracks_the_packaging_include_list(self):
        """The guard must scan every package pyproject actually ships.

        It originally scanned ``src`` and the SDK but not ``governance_core``,
        which is half the shipped artifact and the half most obliged to be
        agnostic. Passing unchecked is not the same as passing.
        """
        import re
        import tomllib
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads((repo / "pyproject.toml").read_text())
        shipped = {
            entry.split(".")[0]
            for entry in pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
        }

        guard = (repo / "scripts/dev/check_fleet_identity_leak.py").read_text()
        block = re.search(r"DEFAULT_PATHS = \(([^)]*)\)", guard).group(1)
        scanned = set(re.findall(r'"([^"]+)"', block))

        missing = {pkg for pkg in shipped if not any(p == pkg or p.startswith(f"{pkg}/") for p in scanned)}
        assert not missing, f"shipped but unguarded: {sorted(missing)}"


class TestTheDoctorExpectsNoResidents:
    """The install doctor ships with no resident roster either.

    Until 2026-09-27 ``scripts/dev/unitares_doctor.py`` carried a hardcoded
    table of one operator's resident LaunchAgents and warned that they were
    "not loaded" on every other install. Which residents a host runs is now
    declared (``UNITARES_DOCTOR_RESIDENT_LAUNCHD``); with nothing declared the
    check has nothing to expect.
    """

    @pytest.fixture
    def doctor(self, monkeypatch):
        import importlib.util
        import sys
        from pathlib import Path

        name = "unitares_doctor_residentless"
        script = Path(__file__).resolve().parents[1] / "scripts/dev/unitares_doctor.py"
        spec = importlib.util.spec_from_file_location(name, script)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)  # dataclasses resolve via sys.modules
        spec.loader.exec_module(mod)
        return mod

    def test_no_resident_launchagent_is_declared_by_default(self, doctor, monkeypatch):
        monkeypatch.delenv(doctor.RESIDENT_LAUNCHD_ENV, raising=False)

        assert doctor.resident_launchd_slots() == ()

    def test_a_launchd_host_with_no_roster_expects_no_residents(self, doctor, monkeypatch):
        # Even on a real launchd deployment, an undeclared roster is SKIP, not
        # a warning about somebody else's residents.
        monkeypatch.delenv(doctor.RESIDENT_LAUNCHD_ENV, raising=False)

        result = doctor.check_resident_agents({doctor.GOVERNANCE_LAUNCHD_LABEL})

        assert result.status == doctor.Status.SKIP


# --- the agent-first Overview (2026-09-27) ------------------------------------
# The dashboard's Overview now leads with agents and hides its resident block
# when the roster is empty (dashboard/tests/landing-agent-first.test.js). What
# it leans on server-side must therefore not depend on the roster: with none
# declared, every agent's check-in still reaches the feed and the verdict
# counts, and nothing is filtered out as "not a resident".

def test_agent_feed_and_checkin_counts_ignore_an_empty_roster(residentless):
    import asyncio
    import time
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from src.broadcaster import broadcaster_instance
    from src.http_routes.telemetry import http_eisv_agents
    from src.http_routes.overview import http_activity

    broadcaster_instance.event_history.clear()
    broadcaster_instance.activity_history.clear()
    for agent_id, decision in (("a", {"action": "proceed"}),
                               ("b", {"action": "proceed", "sub_action": "guide"})):
        asyncio.run(broadcaster_instance.broadcast({
            "type": "eisv_update", "agent_id": agent_id, "agent_name": "agent-" + agent_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "decision": decision,
        }))
    app = Starlette(routes=[Route("/v1/eisv/agents", http_eisv_agents), Route("/api/activity", http_activity)])
    client = TestClient(app, client=("127.0.0.1", 50000))

    feed = client.get("/v1/eisv/agents").json()
    assert sorted(r["agent_id"] for r in feed["agents"]) == ["a", "b"]
    totals = client.get("/api/activity").json()["totals"]
    assert totals == {"proceed": 1, "guide": 1, "pause": 0}


# --- route packs (2026-09-27) -------------------------------------------------
# The reference residents' summary / backlog / adjudication routes are mounted
# only by the opt-in `reference-residents` route pack. Declaring no residents is
# the default install, and it must carry none of those endpoints — independent
# of the roster, which does not mount a pack either way.

def test_default_install_mounts_no_resident_routes(residentless, monkeypatch):
    from starlette.applications import Starlette
    from src.http_api import register_http_routes

    monkeypatch.delenv("UNITARES_ROUTE_PACKS", raising=False)
    app = Starlette()
    register_http_routes(app, server_ready_fn=lambda: True, server_start_time=0.0,
                         server_version="t", has_streamable_http=False)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not [p for p in paths if p.startswith(("/v1/sentinel/", "/v1/watcher/", "/v1/vigil/"))]
    assert "/api/automations" not in paths
    assert "/v1/residents" in paths  # the roster endpoint is core, and answers empty


# --- metrics catalog (2026-09-27) ---------------------------------------------
# GET /v1/metrics/catalog serves the catalog to every install. The metrics the
# reference scraper resident records about this operator's own repo, GitHub org
# and residents live in agents/chronicler/metrics_catalog.json and are
# registered only when UNITARES_METRICS_CATALOG_EXTRA names that file. The
# catalog is built at import, so the default install is checked in a fresh
# interpreter with the variable unset rather than by patching a module global.

def test_default_install_catalog_is_product_only(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "UNITARES_METRICS_CATALOG_EXTRA"}
    out = subprocess.run(
        [sys.executable, "-c",
         "import json; from src.fleet_metrics.catalog import catalog; "
         "print(json.dumps(sorted(catalog)))"],
        cwd=repo, env=env, capture_output=True, text=True, check=True, timeout=60,
    )
    names = json.loads(out.stdout.strip().splitlines()[-1])
    declared = {
        m["name"]
        for m in json.loads(
            (repo / "agents" / "chronicler" / "metrics_catalog.json").read_text()
        )["metrics"]
    }
    assert declared, "the reference extra catalog must declare its metrics"
    leaked = sorted(n for n in names if n.removesuffix(".error") in declared)
    assert not leaked, f"default catalog advertises operator metrics: {leaked}"
    assert "kg.entries.count" in names  # the product layer still ships


def test_an_adopter_service_records_coordination_events(residentless):
    # Migration 072: a residentless install's own agent records coordination
    # events under its own service id. Before it, 035's CHECK admitted only
    # one deployment's six emitter ids, and the sync emitter rewrote any
    # other id to governance_mcp.
    from unittest.mock import patch

    from src.coordination_events import is_valid_service
    from src.coordination_failure_emit import emit_coordination_failure_sync

    assert is_valid_service("my_own_agent")
    written = MagicMock()
    with patch("src.audit_log.audit_logger", written), \
         patch("src.audit_log.AuditEntry") as entry:
        emit_coordination_failure_sync(
            service="my_own_agent",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={},
        )
    assert entry.call_args.kwargs["details"]["service"] == "my_own_agent"
