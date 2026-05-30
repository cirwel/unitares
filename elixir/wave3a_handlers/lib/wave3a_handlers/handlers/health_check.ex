defmodule Wave3aHandlers.Handlers.HealthCheck do
  @moduledoc """
  Wave 3a `health_check` handler — first end-to-end cutover (RFC §5 PR #5).

  Topology, after operator flips `WAVE_3A_HEALTH_CHECK_ON_BEAM=true` in
  `~/.config/cirwel/secrets.env` and restarts the MCP:

      MCP client
          ↓
      Python transport (port 8767)
          ↓ [src/wave3a_routing.py — apply_env_flag_routes set route at boot]
      Python proxy (src/wave3a_beam_proxy.py)
          ↓ [POST http://127.0.0.1:8770/v1/handlers/health_check]
      BEAM listener (this app, port 8770)
          ↓ [HTTPRouter dispatches to Wave3aHandlers.Handlers.HealthCheck.call/1]
      ProbeClient.health_snapshot/0  →  GET /v1/probe/health_snapshot (Python, 8767)
          ↓
      Wave 3a envelope back to BEAM → back through proxy → back to MCP client

  Until the env flag flips, the routing table is empty and the Python
  in-process `handle_health_check` runs unchanged.

  ## Handler contract

  Mirrors the Python `health_check` post-processing at
  `src/mcp_handlers/admin/handlers.py:285-354` — specifically the
  `lite` argument (default true) that strips per-check detail and the
  `_cache` block that summarizes snapshot age/staleness. The Python probe
  at `src/mcp_handlers/wave3a_probe.py::_health_snapshot` returns the FULL
  snapshot (no lite filter) per §2.3, so the lite logic lives here.

  ## Identity gate

  Per RFC §2.4, `health_check` carries `requires_identity="pre_onboard"`.
  The BEAM listener does NOT enforce identity in Wave 3a — the Python
  transport's identity middleware ran before the routing-table lookup, and
  `pre_onboard` tools are exempted there. The pre-cutover script at
  `scripts/ops/wave-3a-pre-cutover-health-check.sh` verifies this attribute
  on the Python side before the operator flips the env flag.

  ## Envelope shape

  Returns a map whose keys are merged flat into the §2.2 success envelope
  by `HTTPRouter`. The router adds `ok: true` and `protocol_version:
  "wave3a.v1"`. The Python proxy at
  `src/wave3a_beam_proxy.py::_validate_success_envelope` validates only
  those two keys; every other key passes through to the MCP client.

  ## §2.6 parity contract

  The keys returned here are intentionally the same set the Python handler
  emits inside its `success_response(...)` payload — `status`, `version`,
  `redis_present`, `identity_continuity_mode`, `status_breakdown`,
  `operator_summary`, `timestamp`, `checks`, `_note`, `_cache`. Volatile
  fields (timestamps, `_cache.age_seconds`, `_cache.produced_at`) are
  masked by the Python-side `mask_timestamps` helper before the golden
  comparison; non-volatile fields must match byte-for-byte across runs.

  ## Failure modes

  The handler degrades to typed-error envelopes on every ProbeClient
  failure atom (timeout, connect_error, probe_token_unset, non_200,
  decode_error, envelope_invalid). Each maps to a stable `error` string so
  golden-fixture matching catches regressions. The Python proxy treats
  every non-2xx and every `ok=false` envelope as a fallback trigger — so
  the worst-case outcome is one extra HTTP round-trip plus a Python
  in-process dispatch, never a silent skip.
  """

  alias Wave3aHandlers.ProbeClient

  @doc """
  Handle a `health_check` dispatch.

  Args:

    * `arguments` — JSON-decoded body from the Python proxy. `"lite"`
      defaults to `true` (matching the Python handler). Any unknown keys
      are ignored — same posture as the Python handler.

  Returns `{:ok, body_map, http_status}`. The HTTP router merges `body_map`
  with `ok: true|false` and the pinned `protocol_version`, then sends the
  result at `http_status`. 200 on success; 5xx on every probe failure
  mode — see RFC §3.2 (the Python proxy treats any non-2xx as a fallback
  trigger).
  """
  @spec call(map()) :: {:ok, map(), pos_integer()}
  def call(arguments) when is_map(arguments) do
    case ProbeClient.health_snapshot() do
      {:ok, envelope} ->
        snapshot_payload = extract_snapshot_payload(envelope)
        lite = lite_arg?(arguments)
        body = build_response(snapshot_payload, lite)
        {:ok, body, 200}

      {:error, :probe_token_unset} ->
        {:ok,
         %{
           error: "service_unavailable",
           reason: "WAVE_3A_PROBE_TOKEN not configured"
         }, 503}

      {:error, :timeout} ->
        {:ok,
         %{
           error: "probe_timeout",
           reason: "Python probe exceeded 500ms budget"
         }, 504}

      {:error, :connect_error} ->
        {:ok,
         %{
           error: "probe_unavailable",
           reason: "could not reach Python probe"
         }, 502}

      {:error, {:non_200, status}} ->
        {:ok,
         %{
           error: "probe_non_200",
           reason: "Python probe returned non-2xx",
           probe_status: status
         }, 502}

      {:error, :decode_error} ->
        {:ok,
         %{
           error: "probe_decode_error",
           reason: "Python probe body was not valid JSON"
         }, 502}

      {:error, :envelope_invalid} ->
        {:ok,
         %{
           error: "probe_envelope_invalid",
           reason: "Python probe response lacked ok: true"
         }, 502}
    end
  end

  # `arguments` defaults to `lite=true` to match the Python handler's
  # `arguments.get("lite", True)`. Accept the JSON boolean and the string
  # forms; anything else is truthy (mirrors Python's default).
  defp lite_arg?(%{"lite" => false}), do: false
  defp lite_arg?(%{"lite" => "false"}), do: false
  defp lite_arg?(%{"lite" => _}), do: true
  defp lite_arg?(_), do: true

  # The probe envelope is `{"ok": true, "protocol_version": "wave3a.v1",
  # "data": {<snapshot fields including _cache>}}` per
  # `wave3a_probe.py::_envelope_ok`. We only need the `data` payload — the
  # wave3a envelope wrapping is the transport concern and is rebuilt by
  # this handler's caller.
  defp extract_snapshot_payload(%{"data" => data}) when is_map(data), do: data
  defp extract_snapshot_payload(envelope), do: envelope

  # Mirrors `src/mcp_handlers/admin/handlers.py:316-353`. The Python probe
  # already added the `_cache` block at the probe surface; we surface it
  # through unchanged. `lite` filters the per-check detail; full response
  # passes the snapshot through verbatim.
  defp build_response(snapshot, true = _lite) do
    response = %{
      "status" => Map.get(snapshot, "status"),
      "version" => Map.get(snapshot, "version"),
      "redis_present" => Map.get(snapshot, "redis_present"),
      "identity_continuity_mode" => Map.get(snapshot, "identity_continuity_mode"),
      "status_breakdown" => Map.get(snapshot, "status_breakdown"),
      "operator_summary" => Map.get(snapshot, "operator_summary"),
      "timestamp" => Map.get(snapshot, "timestamp"),
      "_note" => "Use lite=false for full diagnostic detail"
    }

    full_checks = Map.get(snapshot, "checks", %{})
    lite_checks = filter_lite_checks(full_checks)
    response = Map.put(response, "checks", lite_checks)

    case Map.get(snapshot, "_cache") do
      nil -> response
      cache -> Map.put(response, "_cache", cache)
    end
  end

  defp build_response(snapshot, false = _lite), do: snapshot

  defp filter_lite_checks(checks) when is_map(checks) do
    Map.new(checks, fn
      {name, check} when is_map(check) ->
        entry = %{"status" => Map.get(check, "status", "unknown")}

        entry =
          Enum.reduce(
            ~w(mode redis_present present source_of_truth session_binding_backend),
            entry,
            fn key, acc ->
              case Map.get(check, key) do
                nil -> acc
                value -> Map.put(acc, key, value)
              end
            end
          )

        entry =
          case Map.get(check, "warning") do
            nil -> entry
            warning -> Map.put(entry, "warning", warning)
          end

        entry =
          case Map.get(check, "note") do
            nil -> entry
            note -> Map.put(entry, "note", note)
          end

        {name, entry}

      {name, check} ->
        {name, check}
    end)
  end

  defp filter_lite_checks(other), do: other
end
