# Governed reviewer spawn — the standing agent_spawn producer (v0)

Status: built, inert (flag off). Activation is an operator step (§Activation).
Issue lineage: #1075 (effect-binding residuals), #1252 (producer discovery:
agent_spawn had no standing producer).

## What this is

The dialectic orchestrated-reviewer dispatch
(`src/mcp_handlers/dialectic/orchestrator_dispatch.py`) becomes the fleet's
first *standing* producer of `agent_spawn` governed effects: when
`UNITARES_DIALECTIC_GOVERNED_SPAWN=1`, the reviewer spawn is proposed to the
lease plane (`POST /v1/effects`, `custody_mode=execute`,
`effect_type=agent_spawn`) instead of POSTing the orchestrator directly.

What the governed path adds over the direct path:

- a durable `audit.events` record per spawn (effect_id, proposer, dialectic
  session via `provenance.session_id`);
- idempotent dedup of concurrent same-spec dispatches (two `submit_thesis`
  calls racing on one session no longer yield two reviewers);
- §7/§8 proof material on the wire, so
  `UNITARES_GOVERNED_EFFECT_BINDING_AGENT_SPAWN` becomes flippable with no
  further producer code.

## Honest scope — read before citing this as enforcement

The proposer is a standing dispatcher identity whose continuity token is
minted **in-process by gov-mcp** at dispatch time
(`create_continuity_token(dispatcher_uuid, "dialectic-dispatcher:<session>")`,
a deliberately non-resolvable synthetic sid). gov-mcp is issuer, prover, and
verifier of that credential. Consequences, stated plainly:

- **§7 tier recert passes by construction** for this producer. It proves
  "gov-mcp said so," which was already true. It is not an adversarial gain.
- **§6 behavioral veto is nearly inert** for this producer: the dispatcher
  accrues essentially no new `core.agent_state` rows, so its posture is
  frozen at provisioning. (The provisioning script lands one `sync_state` so
  §6 at least reads a real row instead of the unknown-proposer fail-open
  branch.) The veto still fails closed on a missing/invalid proposer, which
  is what the plane's fail-closed posture actually guarantees here.
- **§8 effect-binding, when flipped, verifies gov-mcp against itself** (mint
  and verify in one process). It exercises the wire format and the veto-side
  gate; it does not close the captured-credential threats (T1/T2) for this
  producer the way it does for an out-of-process one.
- **Residual accepted:** whoever can edit the gov-mcp plist chooses the
  attribution UUID for these spawns. The plist is already the operator trust
  boundary (it holds the token secrets); recording the residual here so audit
  readers know rows with this proposer are server-attributed, not
  agent-attested.

The build is justified on attribution, dedup, and the architectural seam
(spawns flow through `/v1/effects`), plus supply-creates-demand for the
governed surface — not on enforcement.

## Failure routing (load-bearing)

Buckets and rationale live in `governed_spawn.py`'s module docstring; summary:

| Outcome | Condition | Route |
|---|---|---|
| COMMITTED | 202, `status=committed`, fresh `agent_id` | reviewer running, done |
| REFUSED | 403 veto · timeout/error after send · idempotent replay · 409 · canonicalization failure | **in-process synthetic only** — never a direct spawn |
| UNAVAILABLE | connect refused · 501 flag-off · pre-spawn 5xx · 422 producer bug | direct orchestrator path (today's behavior) |
| CONFIG_ERROR | malformed dispatcher UUID · missing bearer/secret | loud log + direct path |

Two of these deserve their reasons pinned:

- **Ambiguous ⇒ never direct.** `agent_spawn` is irreversible. A timeout
  after the request was sent may have spawned; retrying on the direct path
  (where idempotency does not apply) risks a double spawn. The in-process
  synthetic reviewer resolves the session now, and dialectic resolution is
  idempotent, so a late-finishing governed reviewer cannot corrupt it.
- **Canonicalization ⇒ never direct.** The payload embeds thesis text
  authored by the agent under review. Refused C0 controls are stripped
  before hashing (`sanitize_spec_env`), and any residual canonicalization
  failure degrades to synthetic — otherwise the subject of the governance
  check could force the ungoverned path with an escape byte.

## Idempotency key derivation

`dialectic-reviewer:<session_id>:<sha16 of canonical payload>`. The key embeds
the canonical payload hash so the key and the plane's whole-payload digest can
never disagree (the digest covers inherited env such as `PYTHONPATH`; a
session+thesis key would 409 after any environment change). Same spec →
idempotent replay (routed to synthetic, see above); any spec change → new key,
fresh veto + spawn.

Plane-side fix shipped with this change (`governed_effect.ex`): **only
committed rows replay.** Previously a `governance_blocked`/`rejected` row
permanently poisoned its idempotency key — the replay answered 202 with a nil
`agent_id` (readable as success) and the veto was evaluated exactly once per
key. Non-committed prior rows now fall through to a fresh veto + spawn, and
digest conflicts are only enforced against committed rows.

Also shipped plane-side: `orchestrator_spec/1` now forwards `cd` (`python -m`
prepends the child cwd to `sys.path`; without `cd` that is the plane's cwd and
repo modules could be shadowed on name collision).

## Latency budget

`submit_thesis` runs under a 90s tool budget; the pre-existing worst chain
(direct 10s + crash-check 20s + synthetic 55s) is ~85s. The governed leg gets
a hard 5s client timeout and, on timeout, routes to synthetic (not direct), so
the new worst chains are: governed-timeout 5s + synthetic 55s, or governed
fast-refusal (<1s) + the pre-existing 85s. Grant TTL is raised to 120s (mint
default 30s is shorter than the plane's worst-case pre-veto latency).

## Non-changes verified during design

- Reviewer lineage is unchanged: the child env explicitly carries
  `UNITARES_PARENT_AGENT_ID = <paused agent>` and explicit env wins over the
  plane's proposer-derived lineage provisioning (orchestrator
  `provisioned_env/2`). The reviewer also hardcodes its `spawn_reason` at
  onboard, so the provisioned `UNITARES_SPAWN_REASON` candidate is ignored.
- The payload's credential scan (`credential_shaped?`) inspects top-level
  keys only; the reviewer env is nested and credential-free. The scan is not
  what keeps it credential-free — the producer builds that env and must keep
  it so.
- gov-mcp re-enters itself within one request (submit_thesis → plane → veto
  on the same event loop). The await chain yields correctly in principle and
  each leg is timeout-bounded (5s governed leg, 5s veto), but this self-call
  pattern is new: the activation runbook requires a live smoke test under a
  real dialectic before relying on it.

## Activation (operator runbook)

1. Deploy gov-mcp (Python) and lease plane (Elixir — `governed_effect.ex`
   changed: full `mix compile`; plain module, hot-reload eligible).
2. `python3 scripts/ops/provision-dialectic-dispatcher.py` → prints the
   dispatcher UUID.
3. Add to gov-mcp plist (bootout + bootstrap, not kickstart):
   `UNITARES_DIALECTIC_DISPATCHER_UUID=<uuid>`,
   `UNITARES_DIALECTIC_GOVERNED_SPAWN=1`.
4. Trigger one real dialectic; verify a `governed_effect.execute` row with
   `status=committed` and `session_id=<dialectic session>` in `audit.events`,
   and `[DIALECTIC] governed reviewer spawned` in the gov-mcp log.
5. Optionally, later: `UNITARES_GOVERNED_EFFECT_BINDING_AGENT_SPAWN=1` on
   gov-mcp (the producer already attaches grants; the veto then enforces
   them, fail-closed).

Rollback at any step: remove the two plist keys + bootout/bootstrap → the
dispatch reverts to the direct orchestrator path byte-identically.
