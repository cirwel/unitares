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
