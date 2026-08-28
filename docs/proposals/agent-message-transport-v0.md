# Agent message transport v0

**Status: implemented, not yet deployed.** Migration 069 is unapplied on the
live `governance` database (max version 67), and the running lease plane
predates this branch, so `/v1/msg/*` 404s there today. This document is the
contract for those routes. It is deliberately *not* an amendment to
`surface-lease-plane-v0.md`: leases and messages share a node, not a contract.

## 1. Why this exists

Cross-harness agent messages (Claude↔Codex, later Fable) were carried as
governance-KG notes tagged `channel-<topic>` and `to-<agent>`. As delivery that
worked. As substrate it did not, and the failure is structural rather than
incidental: **the KG is a broadcast, durable, full-text-indexed knowledge store,
and a message is addressed and ephemeral.**

Measured on the live governance DB, 2026-08-28:

| property | governance KG | what a message needs |
| --- | --- | --- |
| addressing | `to-claude` is a tag; every searcher sees it | one named recipient |
| read state | none — 45 of 59 channel notes still `status='open'` | delivered / not |
| lifetime | unbounded | expires |
| body | in `summary`, which is FTS-indexed | not searchable by default |
| arbitration | none | one holder per topic |

The concrete damage was displacement, not noise. `scan-actors.sh` — the
collision scan that is supposed to run *first* on any topic — queries
`status='open'` with full-text search, `limit 6`. Because each channel note put
roughly 1.2 KB of message body in the FTS-indexed `summary` column, every
message became a top hit for every topic it mentioned:

| topic probed | channel notes in top 6 |
| --- | --- |
| revenue engine | 6 / 6 |
| dialectic | 6 / 6 |
| preregistration | 6 / 6 |
| review | 5 / 6 |

A genuine open finding on those topics was pushed out of the window entirely.
On 2026-08-27, 49 of 60 KG writes were channel traffic.

## 2. Scope — transport only

This is **neither piece A nor piece B** of
[`agent-channel-wake-gate-v0.md`](agent-channel-wake-gate-v0.md):

- Not piece A. Nothing here long-polls or provides a change feed. `/v1/msg/inbox`
  is a plain request that returns immediately, empty if there is nothing.
- Not piece B. Nothing spawns, wakes, or spends. There is no supervisor, no
  budget, no kill switch, because there is nothing to bound.

Every disconfirmer in that gate is therefore untouched, and its 2026-09-11
observation window is unaffected. **D1 in particular should still be judged on
relays and operator availability, not on message volume**: high traffic on this
transport is evidence about the medium, not demand for wake.

This work also does not cite BEAM Wave 3's authorization (#1822), which
authorises exactly one gate document for the dialectic decision path.

**What it does give a future gate**, without authorising it: the gate names two
missing primitives, and this supplies one of them outright — an **atomic claim**
(`FOR UPDATE SKIP LOCKED` in `/v1/msg/inbox`), so two pollers can never take the
same message. The other, a change feed, remains unbuilt.

## 3. A message carries no authority

Per the gate doc §3b, the operator relay was doing three jobs — wake, authority,
and context selection — and only the first is transport. A recipient of a
message on this transport is **read-and-reply only**. There is deliberately no
authorization-scope column: an agent-authored message cannot grant another agent
operator authority, and a field that looked like it could would invite exactly
that reading.

## 4. Surfaces

### `topic:/` scheme

Registered in `surface_kind_catalog` and added to the `surface_id_grammar`
CHECK, following the migration-050 precedent for `maintenance:/`. Topics are
**lowercased** by the canonicalizer — unlike `resident:/` and `maintenance:/` —
because a topic is typed from prose and `Revenue-Engine` must not become a
second mailbox beside `revenue-engine`.

Registering the scheme makes a topic *leasable*, which is the topic-key gating
the 2026-08-19 coordination dialectic ruled correct ("file leases are the wrong
axis"). **This version does not gate anything on topic leases.** Message send
and inbox work whether or not a topic lease exists; arbitration is a separate,
later change.

### `POST /v1/msg/send`

```
{ topic, sender_agent_uuid, recipient_agent_uuid,
  envelope: {...}, response_to_id?, ttl_s }
→ 200 { ok: true, message: {...} }
```

- The topic is canonicalized **server-side**; the client is not the authority on
  topic identity.
- `reply_depth` is **derived from the parent row**, never taken from the caller,
  and capped at 16. A harness cannot reset its own depth to escape the bound.
- `ttl_s` ≤ 604800 (7 days). The ceiling is what stops this becoming another
  permanently-open note.
- `response_to_id` naming a row that does not exist → **404**, not a silent
  demotion to a new thread.
- A reply must carry its **parent's topic** → **409 `topic_mismatch`**
  otherwise. Without this a third agent can answer into an A↔B thread and
  silently relocate the whole conversation onto a topic its participants are
  not reading.
- UUID-shaped fields are shape-checked before they reach the repo. They would
  otherwise raise `FunctionClauseError` inside `uuid_to_binary/1`, which
  `Plug.ErrorHandler` renders as `503 service_unavailable` — reporting a
  caller's typo as an outage.
- `envelope` is capped at 64 KB, so one message cannot be what fills the table.

### `POST /v1/msg/inbox`

```
{ recipient_agent_uuid, limit? }
→ 200 { ok: true, messages: [...] }
```

The caller *names* an inbox and the identity gate *proves* they are that agent.
The claim and the read are one statement, so concurrent pollers get disjoint
sets. Expired mail is never delivered. The outer `SELECT ... ORDER BY
created_at` is load-bearing: `UPDATE ... RETURNING` has no defined row order, so
the ordering inside the claim CTE decides only *which* rows are taken.

⛔**Delivery is at-most-once, and that is a real limitation, not an oversight
to gloss.** The row is marked `delivered` when the statement commits — before
the HTTP response reaches the recipient. A connection drop or a client crash
between those two moments loses that message permanently: there is no ack, no
visibility timeout, and no redelivery.

At-least-once needs a claim that can expire unacknowledged, which is the
`claimed` / `ack` state machine §6 defers — the same machinery piece B needs.
Building it now would mean building it blind, before any client exists to say
what an ack should mean. So v0 takes the honest trade and names it here:
**this transport is appropriate for coordination traffic that a later poll can
reconstruct (a review round, a status, a pointer to a PR), and NOT for a
message whose loss is silent and unrecoverable.** A sender that needs certainty
should carry the fact in a durable artifact — a PR, an issue, a KG discovery —
and use the message as a pointer to it. That is also what the gate doc observed
about the collaboration it measured: "the single most effective message … was a
pull request."

## 5. Identity posture — verified unconditionally

Both routes call `IdentityBinding.authorize_strict/3`, **not** `authorize/4`.
There is no env flag to turn this off.

`authorize/4` exists so the *lease* surfaces can roll identity binding out
gradually: `:off` skips verification, `:log` verifies, warns, and serves the
request anyway. That is right for a lease — a wrong holder costs a redundant
claim. It is wrong for a mailbox, where the same modes mean handing one agent's
mail to another while writing a warning about it.

⛔**An earlier cut of this change gated the routes on
`identity_binding_mode == :enforce` instead. That was wrong in three ways, and
they are worth recording because each is a trap for the next person:**

1. **Dead on arrival.** The live plane runs `:log`
   (`~/Library/LaunchAgents/com.unitares.lease-plane.plist`, verified
   2026-08-28), so both routes would have returned
   `503 identity_binding_not_enforced` on every call, forever.
2. **The documented remedy was fail-OPEN.** The obvious fix —
   `UNITARES_LEASE_IDENTITY_BOUND_SURFACE_KINDS=topic` — narrows
   `required_for_surface?/1`. A guard reading only the global mode would then
   pass while `authorize/4` short-circuited to `:ok`, serving mail to any
   bearer holder. `docker-compose.yml` already ships that set defaulting to
   `maintenance`, so this was the *shipped* configuration, not a hypothetical.
3. **It would have stood down unrelated telemetry.** Narrowing the surface-kind
   set moves every *other* lease surface from `:log` to `:off` — verification
   stops and the `IdentityMetrics` counters stop advancing. Enabling a mailbox
   with no callers must not silently end the lease identity-binding rollout's
   measurement. (That rollout currently records **3872 of 3872** verifications
   as `invalid`, which is exactly the signal it exists to surface.)

`authorize_strict/3` removes the coupling entirely: the mailbox's
confidentiality boundary no longer depends on how far along an unrelated
rollout happens to be, and no operator action is required to make these routes
safe. A caller without a valid proof gets `403`, in every configuration.

## 6. Storage

`lease_plane.topic_messages`, deliberately **not** `lease_plane_events` and
**not** GenServer state.

- Not `lease_plane_events`: that table is the audit outbox. `forwarded_at` means
  "projected into `audit.tool_usage`" (`audit_outbox_forwarder.ex`), not "read by
  the recipient". Writing messages there would overload an audit marker *and*
  project message traffic into `tool_usage` as phantom throughput — the same
  defect class PR #1955 fixed for lease heartbeats.
- Not GenServer state: `HandoffServer` keeps pending offers in BEAM memory, which
  is right for a sub-minute handoff and wrong for a message. The observed
  collaboration survived a Codex process death precisely because its transport
  was durable.

**Expiry is enforced by a reaper, not only by a read filter.**
`TopicMessageReaper` runs every 60s as a `PeriodicWorker` child, mirroring
migration 067's `IdentityNonceReaper`. ⛔This is load-bearing: an earlier cut
wrote `purge_expired_messages/1` and never wired it, so nothing ever deleted a
row. Filtering `expires_at > now()` on read makes expired mail *invisible*; it
does not make it *gone*. Without the reaper the table accumulates forever —
which is precisely the property this transport indicts the KG for, restated as
rows nothing searches instead of notes nothing closes.

`response_to_id` is `ON DELETE SET NULL`, not the default `RESTRICT`. A reply
routinely outlives the message it answers, and under `RESTRICT` purging that
expired parent raises a foreign-key violation which fails the **whole** purge
batch — so expired mail would accumulate forever and the table would become the
permanently-open note store it exists to replace. This was reproduced as a live
failure before the clause was added. Losing the thread pointer is the right
trade: the parent genuinely is gone, and `reply_depth` is already materialised,
so the loop bound survives it.

Delivery state is `pending → delivered` and nothing else. The gate doc's fuller
envelope also wants `ack` / `claimed` / `completed`; those are **not** added
here, because a claimed-state has no meaning until something can act on a
message unattended — which is piece B, still gated — and a column nothing can
transition is a column whose semantics get invented later by whoever first
writes to it.

## 7. What this does not do

- No wake, no long-poll, no spawn, no change feed.
- No topic-lease gating (the scheme exists; nothing enforces it yet).
- **No at-least-once delivery.** See §4; the failure mode is named there.
- **No client.** There are no MCP verbs, no SDK method, and no Python client;
  `grep -rn "v1/msg"` finds only the Elixir app and these docs. Until a caller
  exists, usage of these routes is necessarily zero, and per CLAUDE.md's
  measurement-authority rule that zero means **"not reachable — built and never
  wired"**, not "no value". ⛔Do not cite `/v1/msg/*` usage as evidence of
  anything until a client ships. Riding this from `start_session` is the
  natural next change.
- No migration of existing KG channel notes. They stay where they are; the
  hygiene fix for `scan-actors.sh` is independent of this transport.
