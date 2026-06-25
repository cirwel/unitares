defmodule UnitaresLeasePlane.GovernanceVetoClient do
  @moduledoc """
  Calls the governance MCP `POST /v1/effect-veto` (governed-effect-plane §6)
  BEFORE an `execute` agent_spawn commits. Governance reads the proposer's
  durable last-decided posture and returns `{vetoed: bool}`.

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
      body =
        Jason.encode!(%{
          "proposer_agent_uuid" => env.proposer_agent_uuid,
          "surface" => Map.get(env, :surface),
          "effect_type" => Map.get(env, :effect_type)
        })

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
