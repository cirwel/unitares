defmodule Wave3aHandlers.Handlers.GetServerInfo do
  @moduledoc """
  Wave 3a `get_server_info` handler — second cutover (RFC §5 PR #6).

  Same topology as `Wave3aHandlers.Handlers.HealthCheck` (PR #5), gated on
  `WAVE_3A_GET_SERVER_INFO_ON_BEAM` in the Python routing table
  (`src/wave3a_routing.py`). The BEAM side calls the Python probe at
  `GET /v1/probe/server_info` and passes the payload through verbatim.

  ## Q2 / FIND-R3 — PID and transport semantics

  RFC §6 Q2 resolved to option 1: **accept Python-PID semantics**. The
  probe payload is built by the same Python function the in-process MCP
  handler uses (`build_server_info_payload` in
  `src/mcp_handlers/admin/handlers.py`), so `current_pid`, `is_current`,
  and `transport` describe the Python backend process — not this BEAM
  listener. This is intentional: the response answers "what is the state
  of the governance MCP server", and that server remains the Python
  process. The probe envelope's `meta.probe_process: true` annotation is a
  probe-surface concern and is dropped here so the payload matches the
  Python handler's `success_response` payload key-for-key (§2.6 parity).

  ## Handler contract

  No arguments are read — mirrors the Python handler, which ignores its
  `arguments` dict entirely. Unknown keys are accepted and ignored.

  ## Envelope shape and failure modes

  Identical to `HealthCheck`: returns `{:ok, body_map, http_status}`; the
  HTTP router stamps `ok` and the pinned `protocol_version`. Probe
  failures map to the shared typed envelopes in
  `Wave3aHandlers.Handlers.ProbeErrors`; the Python proxy treats every
  non-2xx as a fallback trigger, so the worst case is one extra HTTP
  round-trip plus a Python in-process dispatch.
  """

  alias Wave3aHandlers.Handlers.ProbeErrors
  alias Wave3aHandlers.ProbeClient

  @doc """
  Handle a `get_server_info` dispatch.

  `arguments` is accepted for router-signature uniformity and ignored —
  same posture as the Python handler.
  """
  @spec call(map()) :: {:ok, map(), pos_integer()}
  def call(arguments) when is_map(arguments) do
    case ProbeClient.server_info() do
      {:ok, envelope} ->
        {:ok, extract_payload(envelope), 200}

      {:error, reason} ->
        {body, status} = ProbeErrors.body(reason)
        {:ok, body, status}
    end
  end

  # The probe envelope is `{"ok": true, "protocol_version": "wave3a.v1",
  # "data": {<payload>}, "meta": {"probe_process": true}}` per
  # `wave3a_probe.py::_envelope_ok`. Only `data` is the handler payload;
  # `meta` is the FIND-R3 probe-surface annotation and is intentionally
  # not forwarded (the Python handler's payload has no `meta` key).
  defp extract_payload(%{"data" => data}) when is_map(data), do: data
  defp extract_payload(envelope), do: envelope
end
