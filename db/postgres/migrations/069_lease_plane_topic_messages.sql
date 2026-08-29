-- 069_lease_plane_topic_messages.sql
-- Durable, addressed, expiring agent-to-agent message transport.
--
-- WHY THIS EXISTS. Cross-harness messages (Claude<->Codex) were carried as
-- governance-KG notes tagged `channel-<topic>` / `to-<agent>`. That works as
-- delivery and fails as substrate: the KG is a BROADCAST, DURABLE, full-text
-- indexed knowledge store, while a message is ADDRESSED and EPHEMERAL.
-- Measured 2026-08-28: 49 of 60 KG writes on 2026-08-27 were channel traffic,
-- 45 of 59 channel notes were still status='open' with no read-state to close
-- them, and the message BODY lived in `summary` -- the FTS-indexed column --
-- so `scan-actors.sh` (status='open' + FTS, limit 6) returned 6/6 channel
-- chatter on three of four probed topics, displacing real open findings out
-- of its window entirely.
--
-- This table gives messages a home with the properties the KG cannot have:
-- one named recipient, an expiry, a delivery state, and a body in a jsonb
-- column that no full-text search indexes.
--
-- SCOPE -- TRANSPORT ONLY. This is neither piece A (long-poll receive /
-- change feed) nor piece B (spawn-on-message) of
-- docs/proposals/agent-channel-wake-gate-v0.md. Nothing here blocks, wakes,
-- spawns, or spends. That gate's disconfirmers (D1 config, D2 volume, D3
-- spend, D4 lineage, D5 cheap-half, D6 substrate) are untouched and its
-- 2026-09-11 observation window is unaffected. This also does not cite and
-- does not need BEAM Wave 3's signature (#1822), which authorises exactly one
-- gate document for the dialectic decision path.
--
-- A MESSAGE CARRIES NO AUTHORITY. Per the gate doc SS3b(2), the operator relay
-- was doing three jobs -- wake, authority, context selection -- and only the
-- first is transport. A recipient of one of these rows is READ-AND-REPLY ONLY;
-- there is deliberately no authorization-scope column, because an
-- agent-authored message cannot grant another agent operator authority.

-- ⛔Bound the DDL wait. Without lock_timeout, the ALTER below queues behind any
-- long-running transaction on surface_leases and, while queued, blocks every
-- lease operation behind it -- converting a schema change into an outage of
-- unbounded length. Failing fast leaves the migration re-runnable; blocking
-- does not.
SET lock_timeout = '3s';

BEGIN;

-- ---------------------------------------------------------------------------
-- topic:/ surface scheme
-- ---------------------------------------------------------------------------
-- Follows the migration-050 precedent that added `maintenance:/`. A topic is
-- the coordination key the 2026-08-19 coordination dialectic ruled correct
-- ("topic-key gating; file leases are the wrong axis"). Registering the scheme
-- makes `topic:/<key>` a leasable surface so a later change can arbitrate
-- topic ownership through the existing unique-active-holder index. This
-- migration does NOT itself gate anything on topic leases: message transport
-- below is independent of whether a topic lease exists.

INSERT INTO lease_plane.surface_kind_catalog (surface_kind, description)
VALUES
    ('topic', 'agent coordination topics; message-transport addressing and topic-key gating')
ON CONFLICT (surface_kind) DO NOTHING;

-- ⛔ADD CONSTRAINT ... NOT VALID, then VALIDATE separately. 050 used a plain
-- validating ADD, and that precedent covers the SHAPE but not the current size:
-- lease_plane.surface_leases is now 435,438 rows / 126 MB on the live plane. A
-- validating ADD holds ACCESS EXCLUSIVE while it scans every row, so every
-- acquire/renew/heartbeat/release on the plane blocks for the duration -- and
-- here it would do so inside the same transaction as the CREATE TABLE below.
-- NOT VALID takes ACCESS EXCLUSIVE only briefly and does not scan; the separate
-- VALIDATE takes SHARE UPDATE EXCLUSIVE and allows concurrent DML.
--
-- Widening a regex CHECK cannot invalidate an existing row -- every string the
-- old pattern accepted, the new one accepts -- so the window between ADD and
-- VALIDATE admits nothing the old constraint would have refused.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'surface_id_grammar'
          AND conrelid = 'lease_plane.surface_leases'::regclass
    ) THEN
        ALTER TABLE lease_plane.surface_leases DROP CONSTRAINT surface_id_grammar;
    END IF;

    ALTER TABLE lease_plane.surface_leases
        ADD CONSTRAINT surface_id_grammar
        CHECK (surface_id ~ '^(file://|dialectic:/|resident:/|maintenance:/|capture:/|td:/|agent:/|topic:/)')
        NOT VALID;
END $$;

-- ---------------------------------------------------------------------------
-- topic_messages
-- ---------------------------------------------------------------------------
-- Deliberately NOT `lease_plane_events`. That table is the audit outbox:
-- `forwarded_at` means "projected into audit.tool_usage" (see
-- audit_outbox_forwarder.ex), not "read by the recipient". Writing messages
-- there would both overload an audit marker and project message traffic into
-- tool_usage as phantom throughput -- the exact defect class PR #1955 fixed
-- for lease heartbeats.
--
-- Also deliberately NOT GenServer state. HandoffServer keeps pending offers in
-- BEAM memory, which is correct for a sub-minute handoff and wrong for a
-- message: the observed collaboration survived a Codex process death
-- precisely because the transport was durable.

CREATE TABLE IF NOT EXISTS lease_plane.topic_messages (
    message_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    topic                TEXT        NOT NULL,
    sender_agent_uuid    UUID        NOT NULL,
    recipient_agent_uuid UUID        NOT NULL,
    envelope             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- ON DELETE SET NULL, not the default RESTRICT. A reply routinely outlives
    -- the message it answers (short-TTL question, long-TTL answer), and under
    -- RESTRICT the purge of that expired parent fails on the FK -- which fails
    -- the WHOLE purge batch, so expired mail accumulates forever and the table
    -- becomes exactly the permanently-open note store it replaced. Verified as
    -- a live failure before this clause was added; see the purge regression
    -- test in tests/test_migration_069_topic_messages.py. Losing the thread
    -- pointer is the right trade: the parent genuinely is gone, and
    -- reply_depth is already materialised so the loop bound survives.
    response_to_id       UUID        REFERENCES lease_plane.topic_messages (message_id)
                                     ON DELETE SET NULL,
    reply_depth          INTEGER     NOT NULL DEFAULT 0,
    delivery_state       TEXT        NOT NULL DEFAULT 'pending',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ NOT NULL,
    delivered_at         TIMESTAMPTZ,

    -- The DB enforces CANONICAL form, not merely the prefix. `Repo.send_message/1`
    -- is a public function and does not canonicalize; only the HTTP layer does.
    -- A prefix-only CHECK therefore let any non-HTTP caller create
    -- `topic:/Revenue-Engine` alongside `topic:/revenue-engine` -- two mailboxes
    -- for one conversation, which is the exact split-brain the canonicalizer
    -- exists to prevent. Mirrors canonicalize_topic/1: lowercase, no reserved
    -- characters, no trailing slash, non-empty key.
    CONSTRAINT topic_messages_topic_grammar
        CHECK (
            topic ~ '^topic:/[^[:space:]#&?]+$'
            AND topic = lower(topic)
            AND topic NOT LIKE '%/'
        ),

    -- v0 has exactly two states because the inbox read is the only transition
    -- implemented. The gate doc SS3b envelope also wants ack / claimed /
    -- completed; those are NOT added here because a claimed-state has no
    -- meaning until something can act on a message unattended -- that is piece
    -- B, still gated -- and a column nothing can transition is a column whose
    -- semantics get invented later by whoever first writes to it.
    CONSTRAINT topic_messages_delivery_state
        CHECK (delivery_state IN ('pending', 'delivered')),

    -- delivered_at and delivery_state cannot disagree.
    CONSTRAINT topic_messages_delivery_coherent
        CHECK (
            (delivery_state = 'pending'   AND delivered_at IS NULL)
         OR (delivery_state = 'delivered' AND delivered_at IS NOT NULL)
        ),

    -- Ephemeral BY CONSTRUCTION -- the property the KG could not offer. An
    -- unbounded TTL would recreate the permanently-open note.
    --
    -- The bound is a CEILING ONLY, deliberately. An earlier draft also required
    -- expires_at > created_at, which additionally forbade moving a message's
    -- expiry into the past -- i.e. it made retraction impossible and made
    -- "expired mail is never delivered" untestable without waiting out a real
    -- TTL. The invariant that matters is that nothing outlives the ceiling.
    CONSTRAINT topic_messages_ttl_bounded
        CHECK (expires_at <= created_at + INTERVAL '7 days'),

    -- Gate doc SS3b: "a maximum reply depth -- without which two responsive
    -- agents can wake each other in a loop." Enforced at the transport so the
    -- bound holds regardless of which harness is replying.
    CONSTRAINT topic_messages_reply_depth_bounded
        CHECK (reply_depth >= 0 AND reply_depth <= 16),

    -- A message addressed to its own sender is a loop with one participant.
    CONSTRAINT topic_messages_not_self_addressed
        CHECK (sender_agent_uuid <> recipient_agent_uuid),

    CONSTRAINT topic_messages_envelope_is_object
        CHECK (jsonb_typeof(envelope) = 'object')
);

-- The inbox query: one recipient's undelivered, unexpired mail, oldest first.
--
-- Column order is (recipient, created_at) and NOT (recipient, expires_at,
-- created_at). Putting the range predicate on expires_at between the equality
-- column and the sort column destroys the index's ordering guarantee for
-- created_at, so the planner cannot use it to satisfy ORDER BY: measured on
-- 2000 rows, the three-column form was ignored entirely in favour of an
-- expires_at scan plus an explicit Sort, while this form yields a plain
-- ordered index scan with the LIMIT pushed down. expires_at stays a cheap
-- filter on the rows the index already narrowed to.
CREATE INDEX IF NOT EXISTS idx_topic_messages_inbox
    ON lease_plane.topic_messages (recipient_agent_uuid, created_at)
    WHERE delivery_state = 'pending';

-- Threading and per-topic history.
CREATE INDEX IF NOT EXISTS idx_topic_messages_topic
    ON lease_plane.topic_messages (topic, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_topic_messages_response_to
    ON lease_plane.topic_messages (response_to_id)
    WHERE response_to_id IS NOT NULL;

-- Purge scan.
CREATE INDEX IF NOT EXISTS idx_topic_messages_expires_at
    ON lease_plane.topic_messages (expires_at);

COMMENT ON TABLE lease_plane.topic_messages IS
    'Addressed, expiring agent-to-agent messages. Transport only: no wake, no '
    'spawn, no authority. A row grants its recipient nothing beyond the right '
    'to read and reply -- see docs/proposals/agent-channel-wake-gate-v0.md '
    'SS3b(2). Replaces governance-KG channel notes, which broadcast addressed '
    'traffic into a full-text-indexed durable knowledge store.';

COMMENT ON COLUMN lease_plane.topic_messages.envelope IS
    'Message body and metadata. jsonb and NOT full-text indexed on purpose: '
    'the KG failure mode was a message body sitting in the FTS-indexed summary '
    'column, which made every message a top hit for every topic it mentioned.';

COMMENT ON COLUMN lease_plane.topic_messages.delivery_state IS
    'pending -> delivered, set by a recipient-authorized inbox read. This is '
    'NOT lease_plane_events.forwarded_at, which means "projected into '
    'audit.tool_usage" and is unrelated to whether an agent has read anything.';

COMMENT ON COLUMN lease_plane.topic_messages.reply_depth IS
    'Monotone reply-chain depth, capped at 16. Bounds mutual-reply loops at '
    'the transport rather than trusting each harness to stop.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (69, 'lease_plane_topic_messages', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Outside the transaction on purpose: VALIDATE takes SHARE UPDATE EXCLUSIVE and
-- scans the table, and holding that inside the migration transaction would put
-- the lock back for the same duration the NOT VALID split just avoided.
ALTER TABLE lease_plane.surface_leases VALIDATE CONSTRAINT surface_id_grammar;
