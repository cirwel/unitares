# Let active sessions learn that relevant knowledge changed

**Status:** Bounded active-session notices authorized by the operator on 2026-10-13
(“proceed” after the recommendations). Implementation is in PR #2776; pending
validation, merge and migration/deploy. No automatic wake or actor authority.

## Observed gap

The KG audit handoff reached another session in time through a GitHub PR
comment, while the graph acted as a durable notebook. A stored note is not a
delivery receipt. An agent needs a bounded way to notice changes to its own row
or an explicitly addressed row without repeatedly searching the whole graph.

Current source: `src/mcp_handlers/knowledge/handlers.py::_broadcast_knowledge_write`
emits best-effort `knowledge_write` events for stores, batch stores, notes and
promotions. The update path does not call it. Its payload is intended for the
dashboard/bridge and includes a summary, not a durable recipient cursor or
acknowledgement. It therefore does not establish reliable update delivery.

The existing [message transport](agent-message-transport-v0.md) is deployed
according to its dated September 23 re-read. It is not a change feed or wake
system. Its claim-on-read inbox has a known retry/concurrent-consumer concern
(KG `2026-08-29T20:11:28.155684+00:00`, follow-up
`2026-10-03T21:10:01.529367+00:00`); this proposal does not assume durable
at-least-once delivery from it. No live transport probe was performed here.

## Proposed smallest useful path

1. Record a stable change ID and row revision after a successful KG mutation:
   store, update, resolution, archival, supersession and promotion. Transactional
   coupling or a recoverable outbox is required; a failed broadcast must neither
   lose the mutation nor falsely report delivery. Include changed field names,
   actor UUID, owner UUID and an explicit recipient UUID list, not body copies.
2. Match the row's owner and explicit UUID references/subscriptions. A display
   name or a substring in imported prose is not a recipient identity. Define
   whether a newly introduced mention, an already-mentioned row, or both trigger
   notices. A successor is not automatically the predecessor's inbox owner.
3. At an already-active host's next safe interaction, offer up to three compact
   notices: row ID, revision, change kind, actor and a details pointer. Use a
   durable per-process/subscription cursor with bounded retention; replay is
   idempotent and overflow reports a gap requiring a rescan. Opening a result is
   separate from acknowledging the notice. Multiple consumers must not split a
   session's unseen changes silently.
4. Keep the graph canonical. The notice is only a pointer; read current details
   before acting. Do not copy another row's full body into a notification, reopen
   a closed row, create a new finding for each delivery or mutate read status.

This fits the existing search/revise intents in
[INTENT_MAP](../../integration/INTENT_MAP.md), but a feed/subscription or response
field is a new capability and needs a deliberate public-surface design. Prefer
an existing authenticated transport once its cursor semantics are adequate;
do not add a second mailbox merely because best-effort WS events exist.

## Authority, privacy and cost

A notice grants no authority. A mentioned session may read/reply only within
its existing authorization. A malicious row can name another UUID, so recipients
need coalescing, rate/size limits and a mute/unsubscribe option. Verify row-read
authorization when delivering and again when retrieving. A future restricted
row must not leak its existence or summary through this channel.

Default proposal: delivery only while a session is already active, with no
agent spawning, background model call or new check-in. An offline owner keeps
a bounded backlog; expired delivery state reports a gap rather than pretending
the recipient saw everything. Waking a stopped session or spending tokens needs
the separate [wake gate](../archive/agent-channel-wake-gate-v0.md), unchanged here.

Costs are a durable mutation journal/outbox, per-consumer cursor state, retention
and replay logic, a host delivery adapter and tests. Reusing an existing table
is possible only after checking its exact guarantees. A polling interval, byte
budget and retention duration are operator choices before rollout, not measured
defaults invented by this proposal.

## Acceptance and decision

Test mutation success with broadcaster failure, crash between write/delivery,
restart/replay, duplicate notices, two simultaneous consumers, expired cursors,
rapid updates, mention removal, owner versus predecessor, muted sessions and
unauthorized reads. Count created, delivered, acknowledged and opened notices
separately; a zero cannot show disinterest until surfacing, reachability and
recording are checked. Telemetry grants no removal authority.

## Implemented first slice

Migration 074 uses the canonical discoveries table's transaction to maintain a
coalesced recipient/row pointer. It retains the latest revision, not every
intermediate event. UUID revision receipts replace a global increasing cursor:
a transaction committing late cannot fall behind a reader's high-water mark.
An old receipt acknowledges only its exact revision. Two simultaneous reads
see the same pending pointers; neither claims them. A crashed response replays.

Delivery occurs on the next successful, caller-proven tool interaction. It adds
at most three pointers within 2 KiB; `knowledge(action="notices")` reads,
acknowledges or mutes the caller's own inbox. Exact `notify-agent-<UUID>` tags
address a row; ordinary text never does. Removing a mention revokes its pointer.
Fresh successor identities do not inherit predecessor inboxes. Self-authored
updates may also produce owner pointers: the trigger does not invent writer
attribution for mutation paths that lack a proved actor.

Implementation bounds, chosen explicitly for this first rollout: 100 pending
rows per process, seven-day retention, lazy maintenance on write/read, and no
polling timer. Trimming affects only delivery hints, never the KG or historical
audit record. A stable gap token requires a rescan before explicit acknowledgement.
Mute affects display while the bounded backlog remains. The queue stores no
summary, details or model output. SQL reads have a 250 ms statement timeout;
a failed optional delivery preserves the successful primary response and pending
pointer. No adapter background loop, paid model or agent spawn is introduced.

Both AGE and Postgres write the canonical SQL table and therefore hit the same
trigger. The current KG is shared/readable without identity; notices additionally
require a proved recipient and current row membership. A future row ACL must
extend that delivery check; this patch does not claim an ACL system exists.
No preexisting writes are backfilled. This is a change pointer, not a complete
mutation audit or a reliable attribution of the actor; those remain in existing
row/evidence records. These limits replace the journal/actor aspirations above
for the authorized first slice.

Acceptance tests exercise transactional rollback, replay, old-receipt races,
simultaneous readers, cross-recipient receipts, mention removal, no inheritance,
coalescing, bounds, mute, expiry, overflow, delivery failure and identity refusal.
The implementation and contract are reviewable before any production migration.
