# unitares_sdk

Elixir client contract for the UNITARES governance tool bridge.

This is the BEAM counterpart to `agents/sdk` (`unitares-sdk`, Python). Same
layering rule: **the SDK is the contract; the apps under `elixir/` are
reference implementations, not the contract.**

## Why

Seven hand-rolled HTTP clients across three repos talk to governance from the
BEAM, and four of them independently decode the same response envelope —
disagreeing about its shape:

| client | repo | envelope it expects |
|---|---|---|
| `Dispatch.Governance` | `cirwel/dispatch_beam` | `{"result": map}`, and `{"result": json_string}` |
| `AnimaBroker.Governance.Client` | `cirwel/anima-mcp` | plus typed strict-refusal classification |
| `DialecticLive.Governance` | `cirwel/unitares` | `{"result": r}`, then guesses `sessions`/`items`/`data`/list |
| `UnitaresSentinel.GovernanceCheckin` | `cirwel/unitares` | `{"success": true, "result": map}` |

The duplication was already acknowledged in their own comments — `anima_broker`
calls the transport "the pattern every BEAM client uses" and credits its retry
policy to a "(dispatch_beam lesson)".

Copying a lesson by hand is how only **one** of the four guards the strict
identity refusal. That refusal carries neither `success: false` nor an
`action`, so the other three read it as a clean result with no verdict — i.e.
"proceed". After the 2026-06-30 Redis wipe that is exactly how canonical Lumen
stayed governance-dark for ~3 days.

## What it owns, and what it does not

**Owns** — the parts that are identical everywhere and where a missed lesson
costs an outage:

- transport: `POST /v1/tools/call`, timeouts, never raises
- envelope: all four observed shapes, plus refusal and pause classification
- identity: onboard arguments, the lineage anchor file
- config: the opt-in gate that keeps dev shells off the live substrate
- backoff: onboard retry and breaker policy

**Does not own** — these genuinely differ per consumer and centralising them
would be false economy:

- check-in cadence and payload
- EISV mapping
- outcome-event vocabulary
- supervision shape

## Use

Not on Hex, by the same reasoning that keeps the Python SDK off PyPI: GitHub is
already load-bearing for this fleet, and a package index is a new third party.

Pin by `ref`, not `branch` — cross-repo version skew is this fleet's most
reliable failure mode, and two live services already run from different commits
of this repo.

```elixir
{:unitares_sdk,
 git: "https://github.com/cirwel/unitares.git",
 sparse: "elixir/unitares_sdk",
 ref: "<commit-sha>"}
```

```elixir
url    = UnitaresSdk.Config.from_env(Mix.env())
parent = UnitaresSdk.Identity.load_prior_uuid(anchor_path)
args   = UnitaresSdk.Identity.onboard_args(parent, "my_harness")

case UnitaresSdk.Transport.call_tool(url, "onboard", args,
       timeout: UnitaresSdk.Transport.onboard_timeout()) do
  {:ok, result} ->
    {:ok, identity} = UnitaresSdk.Identity.from_onboard(result)
    UnitaresSdk.Identity.persist_anchor(anchor_path, %{"agent_id" => identity.agent_id})

  {:error, {:refused, reason}} ->
    # NOT a transport blip. Do not proceed and do not write a verdict —
    # letting the feed go stale is the honest alarm channel.
    Logger.error("governance refused: #{inspect(reason)}")

  {:error, reason} ->
    Logger.warning("governance unreachable: #{inspect(reason)}")
end
```

## Compatibility

Elixir `~> 1.15`, OTP 27+. The floor is deliberate: `anima_broker` and
`dialectic_live` declare `~> 1.15`, and a contract that forces its own
consumers to upgrade in order to adopt it is not much of a contract. That is
why the SDK uses Erlang's `:json` (OTP 27+) rather than Elixir's `JSON` (1.18+).

Zero third-party runtime dependencies, and CI runs without `mix deps.get` so
the day one appears is the day CI says so.

## Tests

```bash
mix test
```

The suites in `test/envelope_test.exs` under "regression:" pin the two
production outages by shape. They are the reason this package exists; do not
relax them without a live capture showing the server no longer sends that form.
