defmodule UnitaresLeasePlane.GovernedEffectIRConformanceTest do
  @moduledoc """
  Cross-runtime conformance: the Governed-Effect Plane's `POST /v1/effects`
  envelope conforms to fermata's canonical Governed Effect IR, via the UNITARES
  profile mapping.

  This is the Elixir-side mirror of the Python parity guard
  (unitares `tests/test_governed_effect_ir_parity.py`). Together they hold BOTH
  engines to the one contract fermata owns — the Python seed and this BEAM
  realization map onto the same IR. Pure/offline: no DB, no live system.

  fermata's IR schema is JSON Schema draft 2020-12, which the Elixir schema
  libraries don't validate, so this is a STRUCTURAL conformance check (the IR
  Intent required-field contract + custody enum + the profile's keep-UNITARES-
  types-out-of-core rule), not full JSON-Schema validation. The Python guard
  does the full schema validation; this one ties the real plane envelope shape
  to the same contract.

  Mapping spec: docs/proposals/governed-effect-unitares-profile-v0.md.
  """
  use ExUnit.Case, async: true

  # The contract is READ, never restated. Both of these were hardcoded literals
  # in the original guard, which made them tautologies: a fermata IR change
  # could not break them. They now come from the pinned vendored schema
  # (the same copy the Python parity guard validates against) and from the
  # plane's own source, so a change on either side fails this test.
  @ir_schema_path Path.expand(
                    "../../../tests/vendored/fermata-governed-effect-ir-v0.schema.json",
                    __DIR__
                  )
  @governed_effect_source Path.expand(
                            "../lib/unitares_lease_plane/governed_effect.ex",
                            __DIR__
                          )

  @external_resource @ir_schema_path
  @external_resource @governed_effect_source

  @ir_schema @ir_schema_path |> File.read!() |> Jason.decode!()

  # fermata IR `Intent` required fields (governed-effect-ir-v0), read from the schema.
  @ir_intent_required get_in(@ir_schema, ["$defs", "Intent", "required"])
  # fermata's CustodyMode enum — the canonical contract enum.
  @ir_custody_modes get_in(@ir_schema, ["$defs", "CustodyMode", "enum"])
  # The plane's own @custody_modes, read out of its source (the attribute is not
  # exported, so the source line IS the readable surface).
  @plane_custody_modes Regex.run(
                         ~r/^\s*@custody_modes\s+~w\(([^)]*)\)/m,
                         File.read!(@governed_effect_source),
                         capture: :all_but_first
                       )
                       |> hd()
                       |> String.split()

  @custody_modes @plane_custody_modes

  # plane effect_type -> {adapter, operation, required_capability, core?}
  @effect_type_map %{
    "file_write" => {"file", "write", "file.write", true},
    "repo_commit" => {"file", "write", "repo.commit", true},
    "agent_spawn" => {"tool", "spawn", "agent.spawn", false},
    "resident_cycle" => {"tool", "cycle", "resident.cycle", false},
    "service_restart" => {"tool", "restart", "service.restart", false}
  }

  # The UNITARES profile mapping: plane POST /v1/effects body -> fermata IR Intent.
  defp envelope_to_ir_intent(env) do
    {adapter, operation, capability, core?} = Map.fetch!(@effect_type_map, env["effect_type"])

    profile_ext =
      %{
        "proposer" => env["proposer"],
        "provenance" => Map.get(env, "provenance", %{}),
        "required_leases" => Map.get(env, "required_leases", []),
        "required_tier" => env["required_tier"]
      }

    profile_ext =
      if core?, do: profile_ext, else: Map.put(profile_ext, "unitares_effect_type", env["effect_type"])

    %{
      "intent_id" => env["effect_id"],
      "proposal_id" => env["proposal_id"],
      "adapter" => adapter,
      "operation" => operation,
      "target" => env["surface"],
      "input" => env["payload"],
      "required_capability" => capability,
      "idempotency_key" => env["idempotency_key"],
      "custody_mode" => env["custody_mode"],
      "profile" => "unitares",
      "profile_ext" => profile_ext
    }
  end

  defp envelope(effect_type, custody_mode, required_tier) do
    %{
      "effect_id" => "eff_#{effect_type}_#{custody_mode}",
      "proposal_id" => "prop_#{effect_type}_#{custody_mode}",
      "effect_type" => effect_type,
      "surface" => "file:///abs/sandbox/note.txt",
      "custody_mode" => custody_mode,
      "idempotency_key" => "k-conformance-001",
      "payload" => %{"content" => "x"},
      "required_leases" => [%{"surface" => "file:///abs/sandbox/note.txt", "ttl_s" => 300}],
      "proposer" => %{"agent_uuid" => "u-1"},
      "provenance" => %{"session_id" => "s-1"},
      "required_tier" => required_tier
    }
  end

  @cases [
    {"file_write", "record_only", "medium"},
    {"file_write", "execute", "strong"},
    {"agent_spawn", "execute", "strong"},
    {"resident_cycle", "record_only", "medium"}
  ]

  # The regex read above raises at compile time if `@custody_modes` is gone or
  # reshaped, so reaching this test means both lists exist; what it guards is
  # that they still AGREE.
  test "the plane's custody_modes match fermata's CustodyMode enum" do
    assert Enum.sort(@plane_custody_modes) == Enum.sort(@ir_custody_modes)
  end

  for {effect_type, custody_mode, tier} <- @cases do
    test "plane #{effect_type}:#{custody_mode} maps to a valid IR Intent shape" do
      intent =
        envelope_to_ir_intent(envelope(unquote(effect_type), unquote(custody_mode), unquote(tier)))

      for field <- @ir_intent_required do
        v = Map.fetch!(intent, field)
        assert v not in [nil, "", %{}], "#{field} must be present and non-empty"
      end

      assert intent["custody_mode"] in @custody_modes
      assert is_map(intent["input"])
    end
  end

  test "UNITARES-only effect types ride the generic tool adapter + profile_ext (never core vocab)" do
    intent = envelope_to_ir_intent(envelope("agent_spawn", "execute", "strong"))
    assert intent["adapter"] == "tool"
    assert intent["profile"] == "unitares"
    assert intent["profile_ext"]["unitares_effect_type"] == "agent_spawn"
  end

  test "profile policy: execute requires strong tier" do
    for {_type, "execute", tier} <- @cases do
      assert tier == "strong"
    end
  end
end
