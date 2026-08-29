defmodule UnitaresLeasePlane.GovernedEffectIR do
  @moduledoc """
  Production mapping from the lease-plane effect envelope to fermata's
  canonical Governed Effect IR `Intent`, using the UNITARES profile.

  Fermata owns the portable contract. This module owns only the UNITARES
  profile projection: identity/provenance, lease custody, tier requirements,
  and an optional verified shadow-promotion receipt. The full intent is used
  transiently because an execute intent may contain effect bytes; durable
  audit rows receive only the non-secret receipt returned by `receipt/2`.
  """

  alias UnitaresLeasePlane.CanonicalPayload

  @profile "unitares"
  @effect_type_map %{
    "file_write" => {"file", "write", "file.write", true},
    "repo_commit" => {"file", "write", "repo.commit", true},
    "agent_spawn" => {"tool", "spawn", "agent.spawn", false},
    "resident_cycle" => {"tool", "cycle", "resident.cycle", false},
    "service_restart" => {"tool", "restart", "service.restart", false}
  }

  @doc "Map one normalized plane envelope to a fermata IR Intent."
  @spec to_intent(map(), String.t()) :: map()
  def to_intent(env, effect_id) when is_map(env) and is_binary(effect_id) do
    effect_type = Map.fetch!(env, :effect_type)
    {adapter, operation, capability, core?} = effect_mapping(effect_type)

    profile_ext = %{
      "proposer" => proposer(env),
      "provenance" => Map.get(env, :provenance, %{}),
      "required_leases" => Map.get(env, :required_leases, []),
      "required_tier" => required_tier(Map.fetch!(env, :custody_mode))
    }

    profile_ext =
      if core?,
        do: profile_ext,
        else: Map.put(profile_ext, "unitares_effect_type", effect_type)

    profile_ext = maybe_put_promotion(profile_ext, env)

    %{
      "intent_id" => effect_id,
      "proposal_id" => proposal_id(env, effect_id),
      "adapter" => adapter,
      "operation" => operation,
      "target" => Map.fetch!(env, :surface),
      "input" => Map.get(env, :payload, %{}),
      "required_capability" => capability,
      "idempotency_key" => Map.fetch!(env, :idempotency_key),
      "custody_mode" => Map.fetch!(env, :custody_mode),
      "profile" => @profile,
      "profile_ext" => profile_ext
    }
  end

  @doc """
  Build the non-secret durable receipt for a transient fermata Intent.

  The intent digest uses the same cross-language canonical JSON contract as
  effect binding. A value that cannot be canonicalized is rejected before an
  effect is recorded or committed rather than receiving a runtime-local hash.
  """
  @spec receipt(map(), String.t()) :: {:ok, map()} | {:error, term()}
  def receipt(env, effect_id) do
    intent = to_intent(env, effect_id)

    with {:ok, digest} <- CanonicalPayload.sha256(intent) do
      {:ok,
       %{
         "schema" => "fermata.governed-effect-ir.v0",
         "profile" => @profile,
         "intent_id" => intent["intent_id"],
         "proposal_id" => intent["proposal_id"],
         "adapter" => intent["adapter"],
         "operation" => intent["operation"],
         "required_capability" => intent["required_capability"],
         "intent_sha256" => digest
       }}
    end
  end

  defp effect_mapping(effect_type) do
    Map.get_lazy(@effect_type_map, effect_type, fn ->
      operation = effect_type |> String.replace("_", ".")
      {"tool", operation, operation, false}
    end)
  end

  defp proposer(env) do
    case Map.get(env, :proposer_agent_uuid) do
      uuid when is_binary(uuid) and uuid != "" -> %{"agent_uuid" => uuid}
      _ -> %{}
    end
  end

  defp required_tier("execute"), do: "strong"
  defp required_tier("record_only"), do: "medium"

  defp proposal_id(env, effect_id) do
    case Map.get(env, :promotion_receipt) || Map.get(env, :promotion) do
      %{"record_only_effect_id" => predecessor}
      when is_binary(predecessor) and predecessor != "" ->
        predecessor

      _ ->
        effect_id
    end
  end

  defp maybe_put_promotion(profile_ext, env) do
    case Map.get(env, :promotion_receipt) do
      %{} = receipt -> Map.put(profile_ext, "promotion", receipt)
      _ -> profile_ext
    end
  end
end
