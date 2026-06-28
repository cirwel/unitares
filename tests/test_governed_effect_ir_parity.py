"""Schema-parity guard: the Governed-Effect Plane envelope conforms to fermata's
canonical Governed Effect IR, via the UNITARES profile mapping.

This is the anti-drift artifact named in governed-effect-convergence-v0.md — the
guard whose ABSENCE let the fermata seed and this plane diverge (state model,
effect-type vocab, verification, identity coupling) while both claimed the same
primitive. fermata owns the contract; this test holds the plane to it.

The IR schema is a PINNED vendored copy at tests/vendored/ (post convergence
step-1, fermata PR #55 — it has custody_mode). It is a read-only mirror so this
test has no runtime dependency on the fermata package; fermata owns the contract.

Refresh when fermata's IR changes:
    git -C <fermata> show main:references/governed-effect-ir-v0.schema.json \
        > tests/vendored/fermata-governed-effect-ir-v0.schema.json
If a fermata IR bump breaks this test, that is the guard WORKING: the UNITARES
profile mapping must be reconciled.

Mapping spec: docs/proposals/governed-effect-unitares-profile-v0.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

_SCHEMA_PATH = (
    Path(__file__).parent / "vendored"
    / "fermata-governed-effect-ir-v0.schema.json"
)


@pytest.fixture(scope="module")
def ir_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# --- the UNITARES profile mapping (executable spec) --------------------------
# plane effect envelope (governed-effect-plane-v0.md §3) -> fermata IR Intent.
# Kept here as the encoded spec; a runtime mapper module is a later step.
_CORE_EFFECT_TYPES = {
    "file_write": ("file", "write", "file.write"),
    "repo_commit": ("file", "write", "repo.commit"),
}
_UNITARES_EFFECT_TYPES = {
    "agent_spawn": ("tool", "spawn", "agent.spawn"),
    "resident_cycle": ("tool", "cycle", "resident.cycle"),
    "service_restart": ("tool", "restart", "service.restart"),
}


def plane_envelope_to_ir_intent(env: dict) -> dict:
    et = env["effect_type"]
    is_core = et in _CORE_EFFECT_TYPES
    adapter, operation, capability = (_CORE_EFFECT_TYPES if is_core else _UNITARES_EFFECT_TYPES)[et]

    profile_ext: dict = {
        "proposer": env["proposer"],
        "provenance": env.get("provenance", {}),
        "required_leases": env.get("required_leases", []),
        "required_tier": env["required_tier"],
    }
    if not is_core:
        profile_ext["unitares_effect_type"] = et

    return {
        "intent_id": env["effect_id"],
        "proposal_id": env["proposal_id"],
        "adapter": adapter,
        "operation": operation,
        "target": env["surface"],
        "input": env["payload"],
        "required_capability": capability,
        "idempotency_key": env["idempotency_key"],
        "custody_mode": env["custody_mode"],
        "profile": "unitares",
        "profile_ext": profile_ext,
    }


def _envelope(effect_type: str, custody_mode: str, required_tier: str) -> dict:
    return {
        "effect_id": f"eff_{effect_type}_{custody_mode}",
        "proposal_id": f"prop_{effect_type}_{custody_mode}",
        "effect_type": effect_type,
        "surface": "file:///abs/sandbox/note.txt",
        "custody_mode": custody_mode,
        "idempotency_key": "k-parity-001",
        "proposer": {"agent_uuid": "u-1", "client_session_id": "agent-1"},
        "provenance": {"harness": "claude", "session_id": "s-1"},
        "payload": {"content": "x"},
        "required_leases": [{"surface": "file:///abs/sandbox/note.txt", "ttl_s": 300}],
        "required_tier": required_tier,
    }


PLANE_EFFECTS = [
    _envelope("file_write", "record_only", "medium"),   # portable type, shadow
    _envelope("file_write", "execute", "strong"),       # portable type, commit
    _envelope("agent_spawn", "execute", "strong"),      # UNITARES-only type, commit
    _envelope("resident_cycle", "record_only", "medium"),
]


# --- the guard ----------------------------------------------------------------

def test_vendored_schema_is_the_converged_version():
    """The pinned IR must be the post-convergence schema (custody_mode present),
    or the whole parity check is meaningless."""
    schema = json.loads(_SCHEMA_PATH.read_text())
    assert "CustodyMode" in schema.get("$defs", {})
    assert set(schema["$defs"]["CustodyMode"]["enum"]) == {"record_only", "execute"}


@pytest.mark.parametrize("env", PLANE_EFFECTS, ids=lambda e: f'{e["effect_type"]}:{e["custody_mode"]}')
def test_plane_effect_maps_to_valid_ir_intent(ir_validator, env):
    """Every representative plane effect, mapped through the UNITARES profile,
    is a VALID fermata IR Intent — the contract-conformance guarantee."""
    intent = plane_envelope_to_ir_intent(env)
    errors = sorted(ir_validator.iter_errors(intent), key=str)
    assert not errors, f"{env['effect_type']}:{env['custody_mode']} -> {errors[0].message}"


def test_profile_keeps_unitares_types_out_of_core():
    """UNITARES-only effect types ride the generic `tool` adapter + profile_ext,
    never as core IR vocabulary."""
    intent = plane_envelope_to_ir_intent(_envelope("agent_spawn", "execute", "strong"))
    assert intent["adapter"] == "tool"
    assert intent["profile_ext"]["unitares_effect_type"] == "agent_spawn"
    assert intent["profile"] == "unitares"


def test_profile_policy_execute_requires_strong_tier():
    """Profile policy (NOT IR shape): custody_mode=execute must carry tier=strong
    (plane §2). The IR is agnostic; the profile enforces it."""
    for env in PLANE_EFFECTS:
        if env["custody_mode"] == "execute":
            assert env["required_tier"] == "strong", (
                f'{env["effect_type"]} execute must require strong tier'
            )

    # and a malformed execute-at-medium envelope is a profile violation
    def violates(env: dict) -> bool:
        return env["custody_mode"] == "execute" and env["required_tier"] != "strong"

    assert violates(_envelope("file_write", "execute", "medium"))
    assert not violates(_envelope("file_write", "execute", "strong"))
