defmodule Wave3aHandlers.Handlers.DescribeTool do
  @moduledoc """
  Wave 3a `describe_tool` handler — fourth (final) cutover (RFC §5 PR #8),
  completing the §1.1 wave3a handler set (4/4).

  Same topology as the prior three handlers, gated on
  `WAVE_3A_DESCRIBE_TOOL_ON_BEAM` in the Python routing table
  (`src/wave3a_routing.py`). The BEAM side calls the Python probe at
  `GET /v1/probe/describe_tool?tool_name=<name>`.

  ## First arg-reading handler

  `describe_tool` is the FIRST §1.1 handler that consumes a caller argument.
  `health_check` reads `lite` defensively, `get_server_info` and `list_tools`
  ignore their arguments entirely; this handler pulls `"tool_name"` out of the
  `arguments` map (the inner kwargs the Python proxy posts under
  `arguments`, unwrapped by the HTTP router) and forwards it to
  `ProbeClient.describe_tool/1`, which URI-encodes it into the probe query
  string.

  BEAM serves the **`tool_name`-only case**; any other argument delegates to
  the Python in-process handler via the proxy fallback — guarantees no silent
  divergence.

  Argument contract:

    * `arguments` carries any key OTHER than `"tool_name"` (e.g. `lite`,
      `include_schema`, `include_full_description`) → the BEAM path cannot
      faithfully forward those, so it short-circuits with a 422
      `delegated_to_python` envelope BEFORE inspecting `tool_name`. The Python
      proxy treats any non-2xx as a fallback trigger, so the in-process
      `handle_describe_tool(<all kwargs>)` runs and honours every argument —
      byte-parity preserved via the fallback. This guard fires whether or not
      `tool_name` is also present.
    * `arguments` is `tool_name`-only and `"tool_name"` is a non-empty binary
      → forward to the probe. Whitespace-only / unknown names are NOT
      pre-validated here — the Python `handle_describe_tool` owns the
      canonical trim + unknown-tool handling and returns the `error_response`
      payload, which the probe surfaces verbatim (so byte-parity holds on the
      semantic-error path too).
    * `arguments` is `tool_name`-only but `"tool_name"` is absent or empty
      string (or a non-binary) → short-circuit with a 400 `invalid_arguments`
      envelope. The Python proxy falls back, so the in-process
      `handle_describe_tool` then runs and returns the canonical
      `"tool_name is required"` error to the client — byte-parity preserved
      via the fallback, without a pointless probe call.

  Only `tool_name` is ever forwarded; every other argument is single-sourced
  from Python via the fallback. That is the documented parity boundary of
  this slice.

  ## §2.6 parity — single-sourced via the in-process handler

  Like `list_tools`, the payload is built inline inside `handle_describe_tool`
  and wrapped with `success_response` / `error_response`; there is no
  separable builder. The probe CALLS the handler and passes its `TextContent`
  output through verbatim (`src/mcp_handlers/wave3a_probe.py::_describe_tool`),
  with `server_time` masked for byte-determinism.

  ## Envelope shape and failure modes

  Returns `{:ok, body_map, http_status}`; the HTTP router stamps `ok` and the
  pinned `protocol_version`. Probe failures map to the shared typed envelopes
  in `Wave3aHandlers.Handlers.ProbeErrors`.
  """

  alias Wave3aHandlers.Handlers.ProbeErrors
  alias Wave3aHandlers.ProbeClient

  @doc """
  Handle a `describe_tool` dispatch.

  Only `tool_name` is faithfully forwarded by the BEAM path, so:

    * Any argument key other than `"tool_name"` (e.g. `lite`,
      `include_schema`) → 422 `delegated_to_python`, so the Python proxy falls
      back to the in-process handler that honours every argument.
    * `tool_name`-only with a present, non-empty binary → forward to the probe.
    * `tool_name`-only but missing/empty/non-binary → 400 so the Python proxy
      falls back to the in-process handler's canonical "tool_name is required".
  """
  @spec call(map()) :: {:ok, map(), pos_integer()}
  def call(arguments) when is_map(arguments) do
    if has_unforwarded_args?(arguments) do
      {:ok,
       %{
         error: "delegated_to_python",
         reason: "only tool_name is served on the BEAM path; other arguments delegate to Python",
         forwarded: false
       }, 422}
    else
      dispatch(arguments)
    end
  end

  defp dispatch(arguments) do
    case fetch_tool_name(arguments) do
      {:ok, tool_name} ->
        case ProbeClient.describe_tool(tool_name) do
          {:ok, envelope} ->
            {:ok, extract_payload(envelope), 200}

          {:error, reason} ->
            {body, status} = ProbeErrors.body(reason)
            {:ok, body, status}
        end

      :error ->
        {:ok, %{error: "invalid_arguments", reason: "tool_name is required"}, 400}
    end
  end

  # The BEAM path forwards ONLY `tool_name`. If the caller passed any other
  # key, the BEAM handler cannot faithfully serve the request → delegate to
  # Python via a non-2xx (422). True iff `arguments` has a key besides
  # `"tool_name"`.
  defp has_unforwarded_args?(arguments) do
    arguments
    |> Map.keys()
    |> Enum.any?(&(&1 != "tool_name"))
  end

  # `tool_name` must be a present, non-empty binary to be worth a probe call.
  # Empty/absent → :error → 400 → Python fallback emits the canonical
  # "tool_name is required" error_response.
  defp fetch_tool_name(%{"tool_name" => name}) when is_binary(name) and name != "",
    do: {:ok, name}

  defp fetch_tool_name(_), do: :error

  # The probe envelope is `{"ok": true, "protocol_version": "wave3a.v1",
  # "data": {<payload>}}`. Only `data` is the handler payload — both the
  # success and the semantic-error (`success: false`) shapes ride inside it.
  defp extract_payload(%{"data" => data}) when is_map(data), do: data
  defp extract_payload(envelope), do: envelope
end
