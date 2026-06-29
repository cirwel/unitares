-- 053_dialectic_awaiting_facilitation.sql
-- Persist the `awaiting_facilitation` flag on dialectic sessions (#1167 Ask 2).
--
-- Today `awaiting_facilitation` is an in-memory attribute on the live
-- DialecticSession object (src/dialectic_protocol.py), set when a reviewer is
-- stuck and no auto-replacement was found (#1015 routes a rejected proposal
-- here instead of auto-resuming). It is the highest-priority signal for the
-- dialectic live-surface (badge / float-to-top), but `dialectic(action="list")`
-- cannot see it because it lives only in memory and is lost on restart.
--
-- This column brings the persisted row to parity with the in-memory flag so
-- the `list` API can surface it. The `get` path already exposes it via
-- get_awaiting_facilitation_recovery.
--
-- MANUAL migration. Do NOT auto-run. Apply with psql before the server binary
-- that SELECTs core.dialectic_sessions.awaiting_facilitation in
-- list_all_sessions starts. Until applied, the new SELECT errors. Backfilled
-- FALSE for existing rows (the live in-memory flag re-asserts on the next
-- write at each session's reviewer-stuck / reassign site).

ALTER TABLE core.dialectic_sessions
    ADD COLUMN IF NOT EXISTS awaiting_facilitation BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN core.dialectic_sessions.awaiting_facilitation IS
    'TRUE when the reviewer is stuck and no auto-replacement was found, routing '
    'the session to human facilitation (#1015). Highest-priority surface signal. '
    'Mirrors the in-memory DialecticSession.awaiting_facilitation flag (#1167).';
