# Let active sessions learn that relevant knowledge changed

**Status:** Draft capability proposal, 2026-10-13. Operator decision required;
no runtime, notification, wake, message, identity or permission change.

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
the separate [wake gate](agent-channel-wake-gate-v0.md), unchanged here.

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

**Operator question:** Authorize a bounded, replayable change notice for already
active sessions, with explicit UUID recipients and no automatic wake?
