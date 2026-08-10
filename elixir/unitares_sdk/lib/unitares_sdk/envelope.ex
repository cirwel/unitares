defmodule UnitaresSdk.Envelope do
  @moduledoc """
  Decoder for the governance tool-bridge response envelope.

  This module exists because four BEAM clients independently decoded this
  envelope and **disagreed about its shape**. Collected from the live callers:

    * `dispatch_beam` — `%{"result" => map}`, and separately `%{"result" =>
      json_string}` requiring a second decode.
    * `elixir/sentinel` — `%{"success" => true, "result" => map}`; the
      `success` key is load-bearing there and absent in the two above.
    * `elixir/dialectic_live` — `%{"result" => r}` with a raw-body fallback,
      then guesses the payload across `sessions` / `items` / `data` / bare list.
    * `anima_broker` — the only one that classifies a **typed strict refusal**.

  Two production outages are encoded here as behaviour, not comments:

  **1. The silent default-proceed (Lumen, ~3 days dark).** A strict identity
  refusal carries `status=identity_required`, `error_code=SESSION_ERROR`,
  `error_category=auth_error` — and carries NEITHER `success: false` NOR an
  `action`. Every decoder above except `anima_broker`'s reads that as a
  successful result with no verdict, i.e. "proceed". After the 2026-06-30 Redis
  wipe that is exactly how canonical Lumen stayed governance-dark for ~3 days.
  `decode/1` returns `{:error, {:refused, :identity_required}}` for it. A
  caller that pattern-matches `{:ok, _}` cannot silently proceed past a refusal.

  **2. The swallowed pause (Sentinel, ~18h dark).** `AGENT_PAUSED` read as a
  generic tool error is indistinguishable from a transport blip, so a paused
  resident looks merely flaky and nobody investigates. It gets its own tag.

  The contract: `{:ok, map}` means the substrate answered *and did not refuse*.
  Everything else is tagged. There is deliberately no "probably fine" branch.
  """

  @type result :: {:ok, map()} | {:error, term()}

  @refusal_status "identity_required"
  @refusal_codes ~w(SESSION_ERROR IDENTITY_ERROR)

  @doc """
  Decode a raw response body into `{:ok, map}` or a tagged error.

  Accepts every envelope shape observed across the four in-fleet clients, so a
  consumer migrating to this SDK does not have to know which one its endpoint
  happens to speak.
  """
  @spec decode(binary()) :: result()
  def decode(body) when is_binary(body) do
    case json_decode(body) do
      {:ok, term} -> classify(term)
      :error -> {:error, :bad_json}
    end
  end

  @doc """
  Classify an already-decoded term. Exposed for callers that obtained the map
  by another route (and for tests, which is how the outage cases are pinned).
  """
  @spec classify(term()) :: result()
  def classify(%{} = payload) do
    cond do
      # Refusal detection runs BEFORE unwrapping. A refusal can appear at the
      # envelope level or inside "result", and it never sets success: false —
      # that is precisely what made it invisible.
      refusal?(payload) -> {:error, {:refused, refusal_reason(payload)}}
      paused?(payload) -> {:error, {:refused, :agent_paused}}
      Map.get(payload, "success") == false -> {:error, {:tool_error, payload}}
      true -> unwrap(payload)
    end
  end

  def classify(other), do: {:error, {:unexpected_payload, other}}

  # --- unwrapping -----------------------------------------------------------

  defp unwrap(%{"result" => result} = _payload) when is_map(result) do
    # The inner result can itself carry a refusal even when the outer envelope
    # looks clean; re-check rather than trusting the wrapper.
    if refusal?(result) or paused?(result) do
      classify(result)
    else
      {:ok, result}
    end
  end

  defp unwrap(%{"result" => result}) when is_binary(result) do
    # dispatch_beam's case: the bridge sometimes returns tool output as a JSON
    # *string* under "result". A single decode leaves the caller holding a
    # blob it will silently fail to read fields out of.
    case json_decode(result) do
      {:ok, %{} = inner} -> classify(inner)
      # A non-object string result is legitimate output, not a failure.
      {:ok, _scalar} -> {:ok, %{"result" => result}}
      :error -> {:ok, %{"result" => result}}
    end
  end

  defp unwrap(%{"result" => result}), do: {:ok, %{"result" => result}}

  # No "result" key at all: the bridge returned the tool payload directly.
  defp unwrap(%{} = payload), do: {:ok, payload}

  # --- refusal / pause detection -------------------------------------------

  defp refusal?(%{} = m) do
    Map.get(m, "status") == @refusal_status or
      Map.get(m, "error_code") in @refusal_codes or
      Map.get(m, "error_category") == "auth_error"
  end

  defp refusal?(_), do: false

  defp refusal_reason(%{} = m) do
    cond do
      Map.get(m, "status") == @refusal_status -> :identity_required
      Map.get(m, "error_code") == "SESSION_ERROR" -> :session_error
      Map.get(m, "error_code") == "IDENTITY_ERROR" -> :identity_error
      true -> :auth_error
    end
  end

  # ⚠️ `action: "pause"` is NOT checked here, deliberately.
  #
  # `process_agent_update` returns the governance verdict under "action", and
  # "pause" is a perfectly ordinary verdict the caller is supposed to receive
  # and act on. Treating it as a transport-level refusal would convert every
  # legitimate pause verdict into an error and hide it from the code whose job
  # is to handle it — the exact inversion of the bug this module exists to fix.
  #
  # What IS a refusal is `AGENT_PAUSED`: the substrate declining to process the
  # update at all because the agent is already paused. That is the ~18h Sentinel
  # case, and it arrives as an error_code, not as a verdict.
  defp paused?(%{} = m) do
    Map.get(m, "error_code") == "AGENT_PAUSED" or Map.get(m, "status") == "AGENT_PAUSED"
  end

  defp paused?(_), do: false

  # --- json -----------------------------------------------------------------

  # Erlang's :json (OTP 27+) rather than Elixir's JSON (1.18+), so the SDK's
  # Elixir floor stays at ~> 1.15 for consumers that declare it.
  defp json_decode(bin) do
    {:ok, :json.decode(bin)}
  rescue
    _ -> :error
  catch
    _, _ -> :error
  end
end
