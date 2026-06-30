defmodule Wave3aHandlers.Handlers.ListTools do
  @moduledoc """
  Wave 3a `list_tools` handler — third cutover (RFC §5 PR #7).

  Same topology as `Wave3aHandlers.Handlers.GetServerInfo` (PR #6), gated on
  `WAVE_3A_LIST_TOOLS_ON_BEAM` in the Python routing table
  (`src/wave3a_routing.py`). The BEAM side calls the Python probe at
  `GET /v1/probe/list_tools` and passes the payload through verbatim.

  ## §2.6 parity — single-sourced via the in-process handler

  Unlike `get_server_info` (whose payload comes from the standalone
  `build_server_info_payload`), `list_tools` builds its payload inline inside
  the Python MCP handler and wraps it with `success_response`. There is no
  separable builder to share. The probe therefore CALLS `handle_list_tools`
  directly and surfaces its `TextContent` output verbatim
  (`src/mcp_handlers/wave3a_probe.py::_list_tools`), so the probe `data`
  payload is the Python handler's own payload by construction — including the
  `success_response` envelope fields (`success`, `server_time`,
  `agent_signature`). `server_time` is masked on the probe side for
  byte-determinism (§2.6), exactly as `tool_registry` masks its volatile
  fields.

  ## Handler contract

  BEAM serves the **default-argument case only**; any other argument
  delegates to the Python in-process handler via the proxy fallback —
  guarantees no silent divergence.

  No arguments are forwarded. The Python `handle_list_tools` DOES accept
  filter arguments (`tier`, `essential_only`, `lite`, `progressive`), so this
  BEAM cutover serves ONLY the default-argument call (empty `arguments` map):
  the probe invokes `handle_list_tools({})`. If the caller passes ANY argument
  (the `arguments` map is non-empty), this handler does NOT call the probe —
  it returns a 422 `delegated_to_python` envelope. The Python proxy treats
  every non-2xx as a fallback trigger (`wave3a_beam_proxy.py::_call_beam`
  `raise_for_status` → `HTTPStatusError` → `fallback_reason="non_200"`), so
  the in-process `handle_list_tools(<filters>)` then runs and serves the
  correct filtered response. The parity unit (RFC §2.6 "same input") for the
  BEAM path is the default-argument response; every other input is single-
  sourced from Python via the fallback, so there is no silent divergence.

  ## Envelope shape and failure modes

  Identical to `GetServerInfo`: returns `{:ok, body_map, http_status}`; the
  HTTP router stamps `ok` and the pinned `protocol_version`. Probe failures
  map to the shared typed envelopes in
  `Wave3aHandlers.Handlers.ProbeErrors`; the Python proxy treats every
  non-2xx as a fallback trigger, so the worst case is one extra HTTP
  round-trip plus a Python in-process dispatch.
  """

  alias Wave3aHandlers.Handlers.ProbeErrors
  alias Wave3aHandlers.ProbeClient

  @doc """
  Handle a `list_tools` dispatch.

  The BEAM path forwards NO arguments, so it can only faithfully serve the
  default-argument call. An empty `arguments` map → serve from the probe. A
  non-empty `arguments` map (any key present) → return a 422
  `delegated_to_python` envelope so the Python proxy falls back to the
  in-process handler, which honours the filters. This is the safe parity
  guard: the BEAM path never silently serves a default response to a caller
  who asked for something else.
  """
  @spec call(map()) :: {:ok, map(), pos_integer()}
  def call(arguments) when is_map(arguments) and map_size(arguments) > 0 do
    {:ok,
     %{
       error: "delegated_to_python",
       reason: "non-default arguments not served on BEAM path",
       forwarded: false
     }, 422}
  end

  def call(arguments) when is_map(arguments) do
    case ProbeClient.list_tools() do
      {:ok, envelope} ->
        {:ok, extract_payload(envelope), 200}

      {:error, reason} ->
        {body, status} = ProbeErrors.body(reason)
        {:ok, body, status}
    end
  end

  # The probe envelope is `{"ok": true, "protocol_version": "wave3a.v1",
  # "data": {<payload>}}` per `wave3a_probe.py::_envelope_ok`. Only `data`
  # is the handler payload; `list_tools` carries no `meta` annotation.
  defp extract_payload(%{"data" => data}) when is_map(data), do: data
  defp extract_payload(envelope), do: envelope
end
