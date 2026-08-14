-- 063_regime_admit_transition_and_unknown.sql
--
-- Admits two values into core.agent_state.regime's CHECK set:
--
--   TRANSITION  a real basin the live detector emits and the schema forbids
--   unknown     an explicit coercion sink that cannot collide with a real value
--
-- WHY TRANSITION
-- --------------
-- src/monitor_regime.py::detect_regime returns five basins; its own docstring
-- names them: STABLE, CONVERGENCE, TRANSITION, EXPLORATION, DIVERGENCE.
-- TRANSITION ("S falling while I rising", i.e. recovering) is the only one
-- absent from this CHECK. phases.py passes the detector's output to
-- record_agent_state unmodified, whose Python-side allow-list mirrors this
-- constraint — so for the entire history of the table, every TRANSITION
-- reading has been silently coerced to 'nominal' before the INSERT. That is
-- a real dynamical state destroyed on the mainline check-in path.
--
-- Measured consequence (2026-08-13, epoch-3 clean slice): 936 of 3,001
-- consecutive-row regime changes — 31% — are crossings into or out of
-- 'nominal', and every non-synthetic 'nominal' is a coercion casualty
-- (1,374 rows; the 37 genuine ones are bootstrap rows, synthetic=true).
-- Downstream, behavioral_sensor._compute_S weights regime transitions at
-- 0.35 of S, so the manufactured flips perturb a live EISV coordinate.
--
-- WHY 'unknown'
-- -------------
-- The coercion fallback in record_agent_state stays (this constraint is why:
-- an unguarded out-of-set value would fail the INSERT and take the check-in
-- with it), but its sink moves from 'nominal' to 'unknown' in the same
-- release. 'nominal' is a legitimate bootstrap value, so using it as the
-- sink is what made coerced rows unrecognizable. 'unknown' has no other
-- producer; a row carrying it is a coercion event by construction, and
-- state_json.regime_raw (added alongside) holds what was destroyed.
--
-- ORDERING — READ BEFORE DEPLOY
-- -----------------------------
-- Apply this migration BEFORE deploying the code that widens the Python
-- allow-list. New code against the old constraint writes values the DB
-- rejects, and the INSERT failure surfaces as a fleet-wide check-in failure.
-- The reverse order is safe: old code against the new constraint just keeps
-- writing 'nominal', which remains valid.
--
-- DELIBERATELY NOT DONE
-- ---------------------
-- No backfill. The pre-063 coerced rows' original values were destroyed
-- before the INSERT; nothing on disk, in backups, or in the audit stream
-- holds them (verified across state_json, outcome_events.eisv_regime, Redis,
-- data/audit_log.jsonl, and pg_dump history). Rewriting them would assert
-- knowledge that does not exist. The era is identifiable instead:
-- regime='nominal' AND synthetic IS NOT TRUE is a coercion casualty.
--
-- The fossil health values (warning/critical/recovery) are NOT dropped,
-- although they have zero rows ever. Narrowing a CHECK is riskier than
-- widening it, and retiring them is a separate decision with its own
-- migration once the taxonomy question is settled.

BEGIN;

-- Exact name verified against the live catalog before authoring: this is the
-- only CHECK on the regime column. A bare DROP (no IF EXISTS) is deliberate —
-- if the name ever drifts, this migration must fail loudly rather than
-- leave two constraints racing, where the stale one still rejects TRANSITION
-- and the "successful" migration changes nothing.
ALTER TABLE core.agent_state
    DROP CONSTRAINT agent_state_regime_check;

ALTER TABLE core.agent_state
    ADD CONSTRAINT agent_state_regime_check CHECK (regime IN (
        'nominal', 'warning', 'critical', 'recovery',
        'EXPLORATION', 'CONVERGENCE', 'DIVERGENCE', 'STABLE',
        'TRANSITION', 'unknown'
    ));

COMMIT;
