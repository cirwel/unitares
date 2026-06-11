defmodule Wave3aHandlers.Handlers.ProbeErrors do
  @moduledoc """
  Shared mapping from `Wave3aHandlers.ProbeClient` failure atoms to typed
  error envelopes (RFC §3.2).

  Extracted in PR #6 so every handler in the §1.1 list maps probe failures
  to byte-identical `error` / `reason` strings — golden-fixture matching and
  the Python proxy's fallback trigger both key on these literals, so the
  mapping is single-sourced rather than copied per handler.

  Each clause returns `{body_map, http_status}`. The HTTP router stamps
  `ok: false` (any non-200) and the pinned `protocol_version` on top.
  """

  @type probe_error :: atom() | {atom(), any()}

  @spec body(probe_error()) :: {map(), pos_integer()}
  def body(:probe_token_unset) do
    {%{
       error: "service_unavailable",
       reason: "WAVE_3A_PROBE_TOKEN not configured"
     }, 503}
  end

  def body(:timeout) do
    {%{
       error: "probe_timeout",
       reason: "Python probe exceeded 500ms budget"
     }, 504}
  end

  def body(:connect_error) do
    {%{
       error: "probe_unavailable",
       reason: "could not reach Python probe"
     }, 502}
  end

  def body({:non_200, status}) do
    {%{
       error: "probe_non_200",
       reason: "Python probe returned non-2xx",
       probe_status: status
     }, 502}
  end

  def body(:decode_error) do
    {%{
       error: "probe_decode_error",
       reason: "Python probe body was not valid JSON"
     }, 502}
  end

  def body(:envelope_invalid) do
    {%{
       error: "probe_envelope_invalid",
       reason: "Python probe response lacked ok: true"
     }, 502}
  end
end
