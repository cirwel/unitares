-- 054_effects_consumed_nonces.sql
-- Single-use nonce ledger for effect-binding grants (#1075 Phase 1, slice 3).
--
-- Each gnt.v1 grant (src/effect_grant.py) carries a nonce. /v1/effect-veto
-- consumes it exactly once via an atomic INSERT ... ON CONFLICT DO NOTHING
-- (rowcount 1 = first use → allow; 0 = replay → veto). This closes the
-- grant-only slice of T2 (replay) — a captured grant cannot be presented twice.
--
-- Retention rule (design §5): a nonce MUST be retained until at least its
-- grant's exp, or a replay window opens between purge and expiry. We store
-- grant_exp and purge on `grant_exp < now()` (a periodic sweep / opportunistic
-- delete) — never a fixed retention shorter than exp. Purging an already-expired
-- grant's nonce is safe because verify_effect_grant rejects it on exp anyway.
--
-- Lives in the existing `effects` schema (created by 052). MANUAL migration —
-- do NOT auto-run. Apply with psql before the gov-mcp binary that references
-- effects.consumed_nonces starts with UNITARES_GOVERNED_EFFECT_BINDING on.
-- While the binding flag is off (default), the table is simply unused.

CREATE TABLE IF NOT EXISTS effects.consumed_nonces (
    nonce        TEXT PRIMARY KEY,
    grant_exp    TIMESTAMPTZ NOT NULL,
    consumed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Supports the purge sweep (DELETE WHERE grant_exp < now()).
CREATE INDEX IF NOT EXISTS idx_consumed_nonces_grant_exp
    ON effects.consumed_nonces (grant_exp);

COMMENT ON TABLE effects.consumed_nonces IS
    'Single-use ledger for effect-binding grant nonces (#1075). One row per '
    'consumed gnt.v1 grant; INSERT ... ON CONFLICT DO NOTHING enforces single '
    'use at /v1/effect-veto. Purge on grant_exp < now().';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (54, 'effects_consumed_nonces', NOW())
ON CONFLICT (version) DO NOTHING;
