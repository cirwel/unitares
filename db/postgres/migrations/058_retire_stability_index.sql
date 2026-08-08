-- 058_retire_stability_index.sql
--
-- `core.agent_state.stability_index` was retired in commit 20684dd1
-- (2026-03-26) — it was 1.0 − S, superseded by reading `entropy` as S
-- directly. The Python writer has hardcoded a constant ever since, but the
-- column kept its `NOT NULL DEFAULT 0.5`, so the retirement was only ever
-- half-done: every row since April carries a plausible-looking float that
-- was never measured.
--
-- That is the "fails toward healthy, never toward unknown" pattern in its
-- purest form. A SQL consumer running
--
--     SELECT avg(stability_index) FROM core.agent_state ...
--
-- gets 0.0 with sd 0.0 and reads it as "perfectly stable" or "no signal",
-- when the truth is "this has not been measured since March". The value
-- carries no warning; NULL would. `scripts/dev/unitares_doctor.py`
-- (check_signal_degeneracy) already flags the column as `constant`, and has
-- carried it as an expected-on-arrival WARN since 2026-07-30 — this
-- migration is what lets that WARN mean something again.
--
-- The backfill is information-preserving, not a data loss. The value
-- distribution splits cleanly on the retirement date, with no overlap:
--
--     real measurements    1,191 rows   2025-12-12 .. 2026-03-22
--     sentinel 0.0        67,508 rows   2026-04-01 .. (ongoing)
--     sentinel 0.5            37 rows   2026-05-23 .. (bootstrap path)
--
-- The last genuine value predates the retirement commit by four days and
-- the first sentinel follows it by six, so `recorded_at >= '2026-03-26'`
-- separates them with a nine-day margin on both sides. Pre-retirement rows
-- are left untouched — they are real history. Only rows written after the
-- metric stopped being computed are nulled, and only where they still hold
-- one of the two hardcoded constants.
--
-- Deliberately NOT dropping the column: 1,191 rows of genuine
-- pre-retirement measurement live in it, and a DROP would take them. The
-- column becomes write-NULL and read-as-unknown instead.
--
-- Companion Python change removes `stability_index` from the DAO signatures
-- and INSERT column lists, so new rows omit the column entirely and land as
-- NULL.

BEGIN;

ALTER TABLE core.agent_state
    ALTER COLUMN stability_index DROP DEFAULT;

ALTER TABLE core.agent_state
    ALTER COLUMN stability_index DROP NOT NULL;

-- Null the post-retirement sentinels. Bounded by BOTH the retirement date
-- and the two known hardcoded constants, so a real measurement cannot be
-- caught even if the date boundary is wrong.
UPDATE core.agent_state
   SET stability_index = NULL
 WHERE recorded_at >= TIMESTAMPTZ '2026-03-26 00:00:00+00'
   AND stability_index IS NOT NULL
   AND stability_index IN (0.0, 0.5);

COMMENT ON COLUMN core.agent_state.stability_index IS
    'RETIRED 2026-03-26 (commit 20684dd1). Was 1.0 - S; superseded by reading '
    'core.agent_state.entropy as S. NULL means not measured. Rows before '
    '2026-03-26 hold genuine historical measurements; rows after are NULL. '
    'Do not write this column. Read S from entropy, E from state_json->>''E'', '
    'I from integrity, V from volatility.';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (58, 'retire_stability_index', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
