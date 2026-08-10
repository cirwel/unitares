defmodule UnitaresSdk do
  @moduledoc """
  Elixir client contract for the UNITARES governance tool bridge.

  ## Why this exists

  Seven hand-rolled HTTP clients across three repos talk to governance from the
  BEAM, and **four of them independently decode the same response envelope** —
  disagreeing about its shape:

  | client | repo |
  |---|---|
  | `Dispatch.Governance` | `cirwel/dispatch_beam` |
  | `AnimaBroker.Governance.Client` | `cirwel/anima-mcp` |
  | `DialecticLive.Governance` | `cirwel/unitares` |
  | `UnitaresSentinel.GovernanceCheckin` | `cirwel/unitares` |

  The duplication was already acknowledged in their own comments —
  `anima_broker` describes the transport as "the pattern every BEAM client
  uses" and credits its retry policy to a "(dispatch_beam lesson)". Copying a
  lesson by hand is how only one of four clients ended up guarding the strict
  identity refusal that kept Lumen governance-dark for ~3 days.

  This SDK is the shared place for those lessons. It is **transport and
  contract only** — it deliberately does not own check-in cadence, EISV
  mapping, or outcome vocabulary, because those genuinely differ per consumer.

  ## Layout

    * `UnitaresSdk.Transport` — `POST /v1/tools/call`, timeouts, never raises
    * `UnitaresSdk.Envelope` — the four observed shapes, plus refusal and pause
      classification that must not decay into a silent "proceed"
    * `UnitaresSdk.Identity` — onboard arguments and the lineage anchor file
    * `UnitaresSdk.Config` — the opt-in gate that keeps dev shells off the live
      substrate
    * `UnitaresSdk.Backoff` — onboard retry and breaker policy

  ## Consuming it

  Not published to Hex, by the same reasoning that keeps the Python SDK off
  PyPI: GitHub is already load-bearing for this fleet, a package index is a new
  third party. Depend on the subdirectory directly, and pin by `ref` — this
  fleet's most reliable failure mode is cross-repo version skew:

      {:unitares_sdk,
       git: "https://github.com/cirwel/unitares.git",
       sparse: "elixir/unitares_sdk",
       ref: "<commit-sha>"}

  ## Minimal use

      url = UnitaresSdk.Config.from_env(Mix.env())
      parent = UnitaresSdk.Identity.load_prior_uuid(anchor_path)
      args = UnitaresSdk.Identity.onboard_args(parent, "my_harness")

      case UnitaresSdk.Transport.call_tool(url, "onboard", args,
             timeout: UnitaresSdk.Transport.onboard_timeout()) do
        {:ok, result} ->
          {:ok, identity} = UnitaresSdk.Identity.from_onboard(result)
          UnitaresSdk.Identity.persist_anchor(anchor_path, %{"agent_id" => identity.agent_id})
          {:ok, identity}

        {:error, {:refused, reason}} ->
          # NOT a transport blip. Do not proceed, do not write a verdict —
          # letting the feed go stale is the honest alarm channel.
          {:error, reason}

        {:error, reason} ->
          {:error, reason}
      end
  """

  @doc "SDK version, for `spawn_reason` tagging and support questions."
  @spec version() :: String.t()
  def version, do: "0.1.0"
end
