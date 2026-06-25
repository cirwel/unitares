defmodule UnitaresLeasePlane.OrchestratorClient do
  @moduledoc """
  Minimal HTTP client to the **already-live** agent orchestrator (`:8789`,
  `com.unitares.agent-orchestrator`) for governed-effect `agent_spawn` execute.

  Uses Erlang stdlib `:httpc` (no new dependency). Localhost, bearer-gated. The
  orchestrator owns the OS-process spawn, OTP supervision, lease-binding, and
  lineage provisioning — this client only forwards a validated spawn spec and
  returns the assigned `agent_id`. The orchestrator's own bearer (`check_allowed`
  cmd allowlist + `AGENT_ORCHESTRATOR_BEARER_TOKEN`) is the second gate behind
  the lease plane's per-type execute flag; if either the URL or bearer is
  unconfigured we fail closed.
  """

  require Logger

  @doc """
  POST a spawn spec to `<orchestrator>/v1/agents`. Returns the orchestrator's
  assigned `agent_id` on 201, or a typed error. Never raises on transport
  failure — the caller decides effect status from the result.
  """
  @spec spawn_agent(map()) :: {:ok, String.t()} | {:error, term()}
  def spawn_agent(%{} = spec) do
    with {:ok, base} <- base_url(),
         {:ok, bearer} <- bearer() do
      url = String.to_charlist(base <> "/v1/agents")
      headers = [{~c"authorization", String.to_charlist("Bearer " <> bearer)}]
      request = {url, headers, ~c"application/json", Jason.encode!(spec)}
      http_opts = [timeout: timeout_ms(), connect_timeout: 2_000]

      case :httpc.request(:post, request, http_opts, body_format: :binary) do
        {:ok, {{_v, 201, _r}, _h, resp}} ->
          parse_agent_id(resp)

        {:ok, {{_v, status, _r}, _h, resp}} ->
          {:error, {:orchestrator_status, status, truncate(resp)}}

        {:error, reason} ->
          {:error, {:orchestrator_unreachable, reason}}
      end
    end
  end

  defp parse_agent_id(resp) do
    case Jason.decode(resp) do
      {:ok, %{"ok" => true, "agent_id" => id}} when is_binary(id) -> {:ok, id}
      {:ok, other} -> {:error, {:orchestrator_bad_body, other}}
      {:error, _} -> {:error, :orchestrator_bad_json}
    end
  end

  defp base_url do
    case Application.get_env(:lease_plane, :agent_orchestrator_url) do
      url when is_binary(url) and byte_size(url) > 0 -> {:ok, String.trim_trailing(url, "/")}
      _ -> {:error, :orchestrator_url_unset}
    end
  end

  defp bearer do
    case Application.get_env(:lease_plane, :agent_orchestrator_bearer_token) do
      t when is_binary(t) and byte_size(t) > 0 -> {:ok, t}
      _ -> {:error, :orchestrator_bearer_unset}
    end
  end

  defp timeout_ms, do: Application.get_env(:lease_plane, :agent_orchestrator_timeout_ms, 10_000)

  defp truncate(bin) when is_binary(bin), do: binary_part(bin, 0, min(byte_size(bin), 200))
  defp truncate(other), do: inspect(other)
end
