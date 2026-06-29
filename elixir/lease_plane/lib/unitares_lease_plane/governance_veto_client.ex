defmodule UnitaresLeasePlane.GovernanceVetoClient do
  @moduledoc """
  Calls the governance MCP `POST /v1/effect-veto` (governed-effect-plane §6+§7)
  BEFORE an `execute` agent_spawn commits. Governance composes two gates and
  returns `{vetoed: bool}`:

    * §6 — the proposer's durable last-decided verdict/action posture;
    * §7 — strong-tier re-certification of the forwarded
      `proposer_continuity_token`. The token is the proposer's transport-robust
      HMAC proof; governance re-verifies it server-side to the `strong` tier.
      It is forwarded for verification only and is never stored or logged here
      (Invariant 1/7). When the proposer carried no token it is OMITTED from
      the body (not sent as `null`) → governance reads it absent → §7 fails
      closed → vetoed.

  Uses Erlang stdlib `:httpc` (localhost; the governance REST surface bypasses
  auth on trusted networks, so no token is needed for the loopback call).

  FAIL-CLOSED is the caller's job, not ours: this returns `:allow` only on an
  explicit `vetoed:false`. A missing proposer, an unreachable governance MCP, a
  non-200, or a `503` (governance could not read its own state) all return
  `{:error, _}` / `{:blocked, _}`, and `GovernedEffect` treats anything that is
  not `:allow` as `governance_blocked` — an effect is not committed unless
  governance affirmatively cleared it.
  """

  require Logger

  @spec check(map()) :: :allow | {:blocked, String.t()} | {:error, term()}
  def check(%{proposer_agent_uuid: uuid}) when not is_binary(uuid) or uuid == "" do
    # No attributed proposer → governance cannot judge → fail closed. An
    # unattributed RCE-class spawn must not commit.
    {:error, :proposer_required}
  end

  def check(env) do
    with {:ok, base} <- base_url() do
      body = Jason.encode!(build_veto_body(env))

      url = String.to_charlist(base <> "/v1/effect-veto")
      headers = veto_headers()
      request = {url, headers, ~c"application/json", body}
      http_opts = [timeout: timeout_ms(), connect_timeout: 2_000]

      case :httpc.request(:post, request, http_opts, body_format: :binary) do
        {:ok, {{_v, 200, _r}, _h, resp}} -> parse(resp)
        {:ok, {{_v, status, _r}, _h, resp}} -> {:error, {:veto_status, status, truncate(resp)}}
        {:error, reason} -> {:error, {:veto_unreachable, reason}}
      end
    end
  end

  @doc """
  Build the veto request body (pre-encode) from an effect env. §7 proof: the
  proposer's `continuity_token` is forwarded when present, and the key is
  OMITTED entirely when absent/blank — so an unauthenticated proposer is judged
  on a missing token (fail-closed at the veto), never sent an explicit `null`.

  Public for unit-testability (assert forwarding without standing up a server);
  not part of the stable API.
  """
  @spec build_veto_body(map()) :: map()
  def build_veto_body(env) do
    base = %{
      "proposer_agent_uuid" => Map.get(env, :proposer_agent_uuid),
      "surface" => Map.get(env, :surface),
      "effect_type" => Map.get(env, :effect_type)
    }

    base
    |> maybe_put_token(env)
    |> maybe_put_binding(env)
  end

  defp maybe_put_token(body, env) do
    case Map.get(env, :proposer_continuity_token) do
      t when is_binary(t) and t != "" -> Map.put(body, "proposer_continuity_token", t)
      _ -> body
    end
  end

  # §8 effect-binding (#1075): when the proposer carried a grant, forward it PLUS
  # the content fields gov-mcp needs to verify the grant binds THIS effect
  # (payload_sha256/custody_mode/idempotency_key; surface+effect_type already in
  # base). When absent — today, always — the body is byte-identical to the §6/§7
  # request, so this is a pure no-op until proposers start minting grants.
  defp maybe_put_binding(body, env) do
    case Map.get(env, :proposer_effect_grant) do
      g when is_binary(g) and g != "" ->
        body
        |> Map.put("proposer_effect_grant", g)
        |> Map.put("payload_sha256", payload_sha256(env))
        |> Map.put("custody_mode", Map.get(env, :custody_mode))
        |> Map.put("idempotency_key", Map.get(env, :idempotency_key))

      _ ->
        body
    end
  end

  # Canonical payload hash — MUST match the proposer's mint-time payload_sha256
  # so the grant's bound `psha` equals what gov-mcp verifies. Same computation as
  # GovernedEffect.idempotency_digest's payload_hash: sha256 of the JSON-encoded
  # payload, lowercase hex. (Cross-language canonicalization — Elixir Jason vs a
  # Python proposer's json — is the slice-4 wiring integration item; a mismatch
  # fails CLOSED at the veto, never open.)
  defp payload_sha256(env) do
    :crypto.hash(:sha256, Jason.encode!(Map.get(env, :payload, %{})))
    |> Base.encode16(case: :lower)
  end

  defp parse(resp) do
    case Jason.decode(resp) do
      {:ok, %{"vetoed" => true} = b} -> {:blocked, b["reason"] || "vetoed"}
      {:ok, %{"vetoed" => false}} -> :allow
      {:ok, other} -> {:error, {:veto_bad_body, other}}
      {:error, _} -> {:error, :veto_bad_json}
    end
  end

  defp base_url do
    case Application.get_env(:lease_plane, :governance_url) do
      url when is_binary(url) and byte_size(url) > 0 -> {:ok, String.trim_trailing(url, "/")}
      _ -> {:error, :governance_url_unset}
    end
  end

  # Optional bearer for the governance REST surface (loopback bypasses auth, so
  # this is usually unset). Included when configured for non-loopback setups.
  defp veto_headers do
    case Application.get_env(:lease_plane, :governance_api_token) do
      t when is_binary(t) and byte_size(t) > 0 ->
        [{~c"authorization", String.to_charlist("Bearer " <> t)}]

      _ ->
        []
    end
  end

  defp timeout_ms, do: Application.get_env(:lease_plane, :governance_veto_timeout_ms, 5_000)

  defp truncate(bin) when is_binary(bin), do: binary_part(bin, 0, min(byte_size(bin), 200))
  defp truncate(other), do: inspect(other)
end
