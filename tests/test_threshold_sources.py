"""`config(action="get")` must say which thresholds an operator actually set.

`get_thresholds` merges runtime overrides over class defaults and returns bare
numbers, and the handler used to hand that back under the note "These are the
effective thresholds (runtime overrides + defaults)". So an operator reading
`risk_approve_threshold = 0.7` could not tell whether they set it or whether it
shipped that way — and the same response mixes in keys like `void_threshold_min`
that `set_thresholds` refuses outright, with nothing marking the difference.

`describe_thresholds` closes that. The tests below pin the two properties that
make it trustworthy: it agrees with `get_thresholds` on every value, and its
`settable` flag agrees with what `set_thresholds` actually accepts — the drift
that would make the surface lie.
"""

import pytest

from src import runtime_config
from src.runtime_config import (
    OVERRIDABLE_THRESHOLDS,
    clear_overrides,
    describe_thresholds,
    get_thresholds,
    set_thresholds,
)


@pytest.fixture(autouse=True)
def _clean_overrides():
    clear_overrides()
    yield
    clear_overrides()


def test_values_match_get_thresholds_exactly():
    """Two views of one truth; a divergence means one of them is wrong."""
    described = {name: d["value"] for name, d in describe_thresholds().items()}
    assert described == get_thresholds()


def test_defaults_report_as_class_default_and_carry_no_displaced_value():
    for name, detail in describe_thresholds().items():
        assert detail["source"] == "class_default", name
        assert "class_default" not in detail, (
            f"{name}: nothing was displaced, so reporting a displaced default "
            f"would imply an override that does not exist"
        )


def test_override_is_attributed_and_keeps_the_displaced_default():
    original = get_thresholds()["risk_approve_threshold"]
    new_value = round(original / 2, 4)
    assert new_value != original

    result = set_thresholds({"risk_approve_threshold": new_value})
    assert result["success"], result

    detail = describe_thresholds()["risk_approve_threshold"]
    assert detail["value"] == new_value
    assert detail["source"] == "runtime_override"
    assert detail["class_default"] == original, (
        "the displaced shipped value must survive, or the operator cannot see "
        "what their override replaced"
    )

    # Untouched keys must not be dragged along by another key's override.
    assert describe_thresholds()["risk_revise_threshold"]["source"] == "class_default"


def test_settable_flag_matches_what_set_thresholds_accepts():
    """The flag is a promise about the write gate; hold it to that.

    Not a re-read of the same table — this drives the real setter and checks the
    flag predicted the outcome. If the two ever disagree the surface is telling
    operators they can change something they cannot, or the reverse.
    """
    for name, detail in describe_thresholds().items():
        probe = set_thresholds({name: 0.5}, validate=False)
        accepted = probe["success"] and name in probe["updated"]
        assert accepted == detail["settable"], (
            f"{name}: settable={detail['settable']} but set_thresholds "
            f"{'accepted' if accepted else 'refused'} it"
        )
        clear_overrides()


def test_structural_keys_are_reported_unsettable():
    """The mixed set is the trap: these look like the others and are not."""
    described = describe_thresholds()
    for name in ("void_threshold_min", "void_threshold_max", "basin_low_i_ceil"):
        assert name in described, f"{name} vanished from the threshold surface"
        assert described[name]["settable"] is False


def test_overridable_table_is_a_subset_of_the_reported_surface():
    """A settable name absent from the read surface would be unreachable."""
    missing = set(OVERRIDABLE_THRESHOLDS) - set(get_thresholds())
    assert not missing, f"settable but never reported: {sorted(missing)}"


def test_source_vocabulary_stays_off_the_trust_contract_axis():
    """Layer of origin is not §1 provenance; the labels must not blur.

    `docs/trust-contract.md` §1 classifies epistemic status
    (measured / derived / prior-default / unknown). This field reports which
    config layer supplied a number. Borrowing §1's vocabulary here would assert
    an epistemic claim the config layer cannot support — so the tokens are held
    disjoint deliberately, not by accident.
    """
    contract_tokens = {"measured", "derived", "prior", "prior_default", "unknown"}
    emitted = {d["source"] for d in describe_thresholds().values()}
    assert emitted <= {"runtime_override", "class_default"}
    assert not (emitted & contract_tokens)


@pytest.mark.asyncio
async def test_handler_exposes_sources_without_breaking_the_old_shape():
    """`thresholds` stays name -> float; the attribution is additive."""
    import json

    from src.mcp_handlers.admin.config import handle_get_thresholds

    set_thresholds({"risk_approve_threshold": 0.11})

    result = await handle_get_thresholds({})
    body = json.loads(result[0].text)
    data = body.get("data", body)

    assert isinstance(data["thresholds"], dict)
    assert all(isinstance(v, (int, float)) for v in data["thresholds"].values()), (
        "the legacy shape must stay name -> number for existing readers"
    )
    assert data["overridden"] == ["risk_approve_threshold"]
    assert data["sources"]["risk_approve_threshold"]["source"] == "runtime_override"
