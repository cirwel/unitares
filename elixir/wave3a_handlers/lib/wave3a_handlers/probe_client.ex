defmodule Wave3aHandlers.ProbeClient do
  @moduledoc """
  Finch-based client to the Python probe surface at
  `127.0.0.1:8767/v1/probe/*` (PR #1 of Wave 3a).

  Reads `:probe_base_url` and `:probe_timeout_ms` from Application env;
  bearer-auth via `:probe_token` (sourced from `WAVE_3A_PROBE_TOKEN` at
  application start — see `Wave3aHandlers.Application`).

  ## Scope (PR #4 — skeleton only)

  Four functions, one per probe endpoint from RFC §2.3:

    * `health/0`
    * `health_snapshot/0`
    * `server_info/0`
    * `tool_registry/0`

  None are wired into the HTTP router in PR #4. PR #5 connects
  `health_check` end-to-end and tests the round-trip (probe → BEAM →
  Python transport → MCP client) explicitly. Until then this module exists
  so PR #5 lands a single-line dispatch change rather than a new module.

  ## Return shape

  Each function returns `{:ok, map}` on a probe success (HTTP 200 + body
  `ok: true`) and `{:error, reason}` otherwise. The error atoms are
  intentionally machine-readable for the PR #5 dispatch to map onto the
  `coordination_failure.wave_3a.*` event taxonomy:

    * `:probe_token_unset` — `WAVE_3A_PROBE_TOKEN` not configured.
    * `:timeout` — probe call exceeded the 500ms budget (RFC §3.2).
    * `:connect_error` — TCP / DNS / connection-reset failure.
    * `{:non_200, status}` — probe returned a non-2xx status.
    * `:decode_error` — body not valid JSON.
    * `:envelope_invalid` — JSON parsed but body lacked `ok: true`.
  """

  require Logger

  @finch_name Wave3aHandlers.ProbeFinch

  @spec health() :: {:ok, map()} | {:error, atom() | {atom(), any()}}
  def health, do: get("/v1/probe/health")

  @spec health_snapshot() :: {:ok, map()} | {:error, atom() | {atom(), any()}}
  def health_snapshot, do: get("/v1/probe/health_snapshot")

  @spec server_info() :: {:ok, map()} | {:error, atom() | {atom(), any()}}
  def server_info, do: get("/v1/probe/server_info")

  @spec tool_registry() :: {:ok, map()} | {:error, atom() | {atom(), any()}}
  def tool_registry, do: get("/v1/probe/tool_registry")

  defp get(path) do
    case Application.get_env(:wave3a_handlers, :probe_token) do
      token when is_binary(token) and byte_size(token) > 0 ->
        do_get(path, token)

      _ ->
        {:error, :probe_token_unset}
    end
  end

  defp do_get(path, token) do
    base = Application.get_env(:wave3a_handlers, :probe_base_url, "http://127.0.0.1:8767")
    timeout_ms = Application.get_env(:wave3a_handlers, :probe_timeout_ms, 500)
    url = base <> path
    headers = [{"authorization", "Bearer " <> token}, {"accept", "application/json"}]

    request = Finch.build(:get, url, headers)

    case Finch.request(request, @finch_name, receive_timeout: timeout_ms) do
      {:ok, %Finch.Response{status: 200, body: body}} ->
        decode_envelope(body)

      {:ok, %Finch.Response{status: status}} ->
        {:error, {:non_200, status}}

      {:error, %Mint.TransportError{reason: :timeout}} ->
        {:error, :timeout}

      {:error, %Mint.TransportError{}} ->
        {:error, :connect_error}

      {:error, _other} ->
        {:error, :connect_error}
    end
  rescue
    # Finch.build/Finch.request can raise on malformed URLs / pool init —
    # keep the contract uniform so the caller never sees a raise.
    exc ->
      Logger.debug("[wave3a_handlers] probe call raised: #{inspect(exc)}")
      {:error, :connect_error}
  end

  defp decode_envelope(body) do
    case Jason.decode(body) do
      {:ok, %{"ok" => true} = decoded} ->
        {:ok, decoded}

      {:ok, _} ->
        {:error, :envelope_invalid}

      {:error, _} ->
        {:error, :decode_error}
    end
  end
end
