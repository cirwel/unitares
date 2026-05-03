---
status: PLANNING (PR 1 scope frozen)
target_branch: impl/lease-plane-phase-a
based_on: docs/lease-plane-v0.8 (commit 31ada78)
rfc_baseline: docs/proposals/surface-lease-plane-v0.md @ v0.8
authored: 2026-04-30
---

# Surface Lease Plane — Phase A Implementation Plan

The v0.8 RFC names **27 Phase A test gates + 3 v0.7 implementation drift items + 2 migrations (026, 027)**. Landing all of those in one PR would produce an unreviewable blob. This plan groups them into focused PRs so each one has a tight RFC↔code mapping that a reviewer can audit row-by-row.

**Methodology commitment:** every Phase A PR description MUST include the four-column table below — `RFC gate → code surface → test name → status` — for exactly the rows it implements. Other rows must be marked `(deferred to PR N)`. No PR may exceed ~10 rows in this table without operator approval.

## PR 1 — Storage + model drift (this PR)

Scope: the two migrations + the three v0.7 drift fixes + the tests that prove only those changes. Everything else is deferred to PR 2+. The reason this group lands first: migration 026's generated-column conversion will hard-fail the Elixir router unless drift fix 2 lands in the same PR; the v0.8 RFC §11 explicitly names this coupling. Drift fix 1 (extended `AcquireHeldByOther`) and drift fix 3 (Sentinel alarm rule) are bundled because they are small, isolated, and otherwise risk being forgotten.

### Out of scope for PR 1 (deferred)

- The §7.12 canonicalization helper (`src/lease_plane/canonicalize.py`) and its 6 test gates.
- The §7.11 deprecation CLI (`lease-plane deprecate / deprecation-sweep / deprecation-finalize`) and its 6 test gates.
- The §7.3.2 retry-with-backoff client method (`acquire_with_retry`).
- The HTTP transport HTTP-409 body-parse test (`test_urllib_transport_parses_409_body`).
- The §7.10 `LEASE_FORCE_RELEASE_TOKEN` integration test (already named in §9; lands with the deprecation CLI in PR 3).

### PR 1 — RFC gate → code surface → test name → status table

| RFC gate | Code surface | Test name | Status |
|----------|--------------|-----------|--------|
| §7.2.2 storage CHECK on `surface_id` scheme grammar | `db/postgres/migrations/026_lease_plane_grammar.sql` (new) | `test_migration_026_grammar_check_rejects_invalid_scheme` (Python pytest hitting live DB) | TODO |
| §7.2.3 generated `surface_kind` column | same migration 026 (DROP COLUMN + ADD COLUMN ... GENERATED ALWAYS AS STORED) | `test_migration_026_surface_kind_is_generated_column` | TODO |
| §7.2.3 caller cannot supply conflicting `surface_kind` | same migration 026 (generated column is read-only at INSERT) | `test_surface_kind_insert_with_conflicting_value_raises` | TODO |
| §7.11.1 `deprecated_schemes` + `surface_kind_catalog` tables | `db/postgres/migrations/027_lease_plane_deprecation.sql` (new) | `test_migration_027_deprecated_schemes_table_exists` | TODO |
| §7.11.1 catalog seeded with 5 schemes (file/dialectic/resident/capture/td) | same migration 027 (`INSERT INTO surface_kind_catalog ...`) | `test_migration_027_surface_kind_catalog_seeded` | TODO |
| §7.3.2 `AcquireHeldByOther` extended typed-absence shape (v0.7 drift fix 1) | `src/lease_plane/models.py` — add `surface_id`, `blocking_lease_id`, `retry_after_hint_ms` fields | `test_held_by_other_includes_v0_7_extended_fields` | TODO |
| §7.2.3 router does not accept `surface_kind` in acquire body (v0.7 drift fix 2) | `elixir/lease_plane/lib/unitares_lease_plane/http_router.ex` — drop `"surface_kind"` from required + params map in `extract_acquire_params` | Elixir-side: `test http_router rejects surface_kind in acquire body after migration 026` | TODO |
| §7.10 + §7.11.5 Sentinel `event_type='forced'` alarm rule wired (v0.7 drift fix 3) | (deferred to PR 3) | (deferred) | DEFERRED — depends on PR 3 force-release CLI to have something to fire on, and on adding direct DB access to Sentinel which is new architectural surface |

**Total: 7 rows in PR 1; one row deferred to PR 3.**

### Specific implementation notes

#### Migration 026 sequencing concern

The v0.8 RFC §7.2.3 names migration 026 as `ALTER TABLE ... DROP COLUMN surface_kind; ALTER TABLE ... ADD COLUMN surface_kind text GENERATED ALWAYS AS (split_part(surface_id, ':', 1)) STORED`. Live-verifier confirmed `surface_leases` is empty in production, so the DROP is safe. **Pre-flight check the migration MUST do:** if any rows exist, abort with a clear error pointing at the v0.8 RFC §7.2.3 fallback (CHECK-pair option). Do not silently DROP a populated column.

The grammar CHECK regex from v0.8 §7.2.2: `surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/)'`. Note the `file://` is double-slash; others are single-slash. This is intentional per §7.2.1 (file:// preserved for filesystem-URI tradition).

#### Migration 027 schema

`surface_kind_catalog` is the first-class registry; `deprecated_schemes.surface_kind` FKs into it. The v0.8 RFC §7.11.1 sketches the table; reviewer should compare the migration to that sketch row-by-row. Note `drain_window_days` CHECK constraint: `> 0 AND <= 90`.

#### `AcquireHeldByOther` model change

Currently in `src/lease_plane/models.py`:
```python
class AcquireHeldByOther(BaseModel):
    ok: Literal[False]
    error: Literal["held_by_other"]
    held_by_uuid: UUID
    expires_at: datetime
```

After this PR (per v0.8 §7.3.2 contract):
```python
class AcquireHeldByOther(BaseModel):
    ok: Literal[False]
    error: Literal["held_by_other"]
    surface_id: str
    blocking_lease_id: UUID
    held_by_uuid: UUID
    expires_at: datetime
    retry_after_hint_ms: int
```

The Elixir router must populate the new fields; that's a separate small router edit in this PR. `_parse_acquire` in `client.py` does not need changes — Pydantic's `model_validate` will pick up the new fields automatically.

#### `extract_acquire_params` change

Today (per `http_router.ex` line 191): `required = ["surface_id", "surface_kind", "holder_agent_uuid", "holder_kind", "ttl_s"]`. After PR 1: drop `"surface_kind"` from required and from the params map at line 216. The Repo INSERT relies on the generated column; supplying `surface_kind` would raise `ERROR: column "surface_kind" is a generated column`.

#### Sentinel alarm rule

Currently no rule keyed on `release_reason` or `event_type='forced'` exists in `agents/sentinel/agent.py` (live-verifier Finding 5 SOURCE_ONLY). Add a rule that:

1. Polls `lease_plane.lease_plane_events` for `event_type='forced'` since last poll.
2. For events where the corresponding lease's `release_reason='forced'` AND `event_type != 'lease.deprecation_swept'`: emit one alarm per event (per §7.10 alarm-on-every-event rule).
3. For events where `event_type='lease.deprecation_swept'`: group by `deprecation_id`, emit one summary alarm per `deprecation_id` after `sweep_completed_at` is set on the corresponding `deprecated_schemes` row (per §7.11.5 batch suppression).

The rule needs `lease_plane_events` SELECT access. If Sentinel doesn't already have it, the migration step adds the GRANT.

## PR 2 — §7.12 canonicalization helper (this branch: impl/lease-plane-phase-a-pr2-canonicalize)

Scope shipped:
- `src/lease_plane/canonicalize.py` (new) implementing the §7.12.1 server-side rule: tmpfile probe (closes live-verifier DRIFT-3), double-realpath (closes DRIFT-2), per-scheme dispatch, `capture:/` member sorting, `dialectic:/` lowercase, `resident:/` reserved-character rejection.
- Helper error semantics (§7.12.2): `CanonicalizeError` exception with `reason` codes (`symlink_loop`, `path_too_long`, `invalid_scheme`).
- AcquireRequest `surface_kind` field removed (closes the v0.7 PR 1 oversight — Elixir router was updated but Python schema wasn't).
- `lease_advisory_scope()` `surface_kind` parameter softened to optional + ignored (preserves backwards-compat for watcher/vigil/sentinel/chronicler/ship.sh callers without surface_id migration in this PR).
- 4 of the 6 §7.12 Phase A test gates pass.

Deferred to **PR 2.5** (see new section below):
- Pydantic `field_validator` on `AcquireRequest.surface_id` (would brick production agents whose surface_ids are non-canonical).
- 2 of the 6 §7.12 Phase A test gates marked `pytest.mark.skip`.

### PR 2 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.12.1 step 4 — tmpfile probe (not pathconf) | `src/lease_plane/canonicalize.py::_detect_case_insensitive` | `test_canonicalize_case_detection_uses_tmpfile_probe` | DONE |
| §7.12.1 step 2 — double-realpath for /var → /private/var | `canonicalize.py::_canonicalize_file` | `test_canonicalize_resolves_var_to_private_var_on_macos` | DONE |
| §7.12.1 capture:/ member sorting | `canonicalize.py::_canonicalize_capture` | `test_capture_canonicalizes_member_ordering` | DONE |
| §7.12.2 helper error semantics (symlink loop, NUL, nonexistent) | `canonicalize.py::canonicalize` | `test_canonicalize_error_semantics` | DONE |
| §7.12.5 AcquireRequest field_validator wired | (deferred to PR 2.5) | `test_acquire_request_surface_id_field_validator_wired` | SKIPPED |
| §7.12.4 Pydantic ?-rejection | (deferred to PR 2.5) | `test_acquire_request_rejects_query_string_in_surface_id` | SKIPPED |
| §7.2.3 Pydantic AcquireRequest drops `surface_kind` (closes PR 1 oversight) | `src/lease_plane/models.py`, `src/lease_plane/advisory.py`, `scripts/dev/_ship_lease_advisory.py` | (covered by 39-test lease-plane regression) | DONE |

## PR 2.5 — Production-agent surface_id migration + AcquireRequest field_validator (this branch: impl/lease-plane-phase-a-pr2-5-validator)

**Surfaced during PR 2 implementation:** the production agents (watcher, vigil, sentinel, chronicler, ship.sh advisory) used surface_ids like `watcher:scan_commits:<repo>`, `vigil:cycle`, `sentinel:cycle`, `chronicler:scrape`, `ship.sh:test-branch` — none of which match the canonical scheme list (RFC §7.2.1). Wiring the AcquireRequest field_validator without first migrating these would cause every agent to crash on first acquire with `ValidationError: scheme not in canonical scheme list`. PR 2.5 lands migration + validator atomically.

### PR 2.5 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.12.5 AcquireRequest field_validator wired | `src/lease_plane/models.py::_validate_surface_id` | `test_acquire_request_surface_id_field_validator_wired` | DONE |
| §7.12.4 Pydantic `?`-rejection | same field_validator | `test_acquire_request_rejects_query_string_in_surface_id` | DONE |
| §7.2.1 production agents on canonical schemes | `agents/{watcher,vigil,sentinel,chronicler}/agent.py` + `scripts/dev/ship.sh` | `agents/{watcher,vigil,sentinel}/tests/test_lease_advisory.py`, `tests/test_chronicler_lease_advisory.py` (assertions updated to canonical surface_id + `surface_kind not in captured`) | DONE |
| §7.2.3 `lease_advisory_scope()` drops `surface_kind` parameter | `src/lease_plane/advisory.py` | (lease-plane regression suite + 22 agent advisory tests) | DONE |
| Test fixtures use canonical schemes | `tests/test_lease_plane_advisory.py`, `tests/test_ship_lease_advisory.py`, `tests/test_chronicler_lease_advisory.py` | (existing tests, surface_id values updated) | DONE |

Per-agent migration mapping (committed):
- `watcher:scan_commits:<repo>` → `resident:/watcher_scan_commits_<sanitized>` (slashes → underscores)
- `vigil:cycle` → `resident:/vigil_cycle`
- `sentinel:cycle` → `resident:/sentinel_cycle`
- `chronicler:scrape` → `resident:/chronicler_scrape`
- `ship.sh:<branch>` → `resident:/ship_sh_<branch>` (slashes preserved; branch names with `/` like `feat/foo` produce `resident:/ship_sh_feat/foo`)

Rationale for splitting from PR 2: the canonicalize.py helper is reviewable independently; coupling it to a fleet-wide surface_id migration risked both reviews stalling on the wider impact. PR 2 shipped the helper; PR 2.5 ships the migration + validator together — atomic landing is the safety contract (validator without migration = bricked fleet; migration without validator = silently inconsistent).

## PR 3a — §7.11 deprecation CLI (this branch: impl/lease-plane-phase-a-pr3a-deprecate-cli)

Scope shipped:
- `scripts/dev/lease_plane_deprecate.py` standalone Python CLI implementing the 4-phase operator-driven deprecation procedure (RFC §7.11.2):
  - `deprecate <kind>` — Phase 0: serializable transaction + advisory lock + INSERT into `deprecated_schemes` (idempotent via `ON CONFLICT DO NOTHING`).
  - `deprecation-sweep <kind>` — Phase 2: idempotent force-release of surviving leases per RFC §7.11.4 predicate (`WHERE released_at IS NULL AND surface_kind = $1 FOR UPDATE SKIP LOCKED`); emits `lease.deprecation_swept` events with `deprecation_id` payload for batch correlation.
  - `deprecation-finalize <kind>` — Phase 3: records `check_migrated_at` (the actual ALTER TABLE drop is operator-issued in same session per §7.11.2).
  - `deprecation-status [<kind>]` — operator visibility into `deprecated_schemes` table.
- Force-release authorization gated on `LEASE_FORCE_RELEASE_TOKEN` from env or `~/.config/cirwel/secrets.env`. `GOVERNANCE_TOKEN` does NOT authorize (RFC §7.10).
- Migration 028 (`db/postgres/migrations/028_lease_plane_trigger_fix.sql`): drops the now-redundant `surface_kind IS DISTINCT FROM` check from `lease_plane.enforce_immutable_lease_fields()`. Surfaced during PR 3a TDD — after migration 026 made `surface_kind` a generated column, the BEFORE UPDATE trigger sees `NEW.surface_kind = NULL` (generated values populate AFTER triggers fire), bricking ANY UPDATE on `surface_leases` including the §7.11 sweep. `surface_id` immutability is unchanged and transitively guards the derived `surface_kind`.

### PR 3a — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.11 Phase 0 INSERT into deprecated_schemes | `scripts/dev/lease_plane_deprecate.py::deprecate_cmd` | `test_deprecate_cli_phase_0_inserts_row` | DONE |
| §7.11 Phase 0 idempotent (ON CONFLICT DO NOTHING) | same | `test_deprecate_cli_idempotent_no_duplicate_row` | DONE |
| §7.11.1 Phase 0 unknown-kind rejected at catalog FK | same | `test_deprecate_cli_unknown_kind_rejected` | DONE |
| §7.11.4 sweep predicate idempotent on partial-failure re-run | `deprecation_sweep_cmd` | `test_deprecation_sweep_predicate_idempotent` | DONE |
| §7.11.3 sweep emits `lease.deprecation_swept` events with deprecation_id | same | `test_deprecation_sweep_emits_lease_deprecation_swept_events` | DONE |
| §7.11.2 Phase 3 records check_migrated_at | `deprecation_finalize_cmd` | `test_deprecation_finalize_records_check_migrated_at` | DONE |
| §7.10 sweep requires `LEASE_FORCE_RELEASE_TOKEN`; rejects `GOVERNANCE_TOKEN` | sweep auth path | `test_deprecation_sweep_requires_force_release_token` | DONE |
| §7.11.7 race-window mitigation (serializable tx + advisory lock) | `deprecate_cmd` SQL | (covered by serializable transaction wrapping in implementation; full concurrent-acquire test deferred to PR 3b once Sentinel alarm + lease acquire path are in scope together) | PARTIAL |
| Migration 028 trigger fix | `db/postgres/migrations/028_lease_plane_trigger_fix.sql` | (covered transitively by sweep tests — they UPDATE `surface_leases` and would fail without 028) | DONE |

## PR 3b — §7.10/§7.11.5 Sentinel forced-release alarm rule (this branch: impl/lease-plane-phase-a-pr3b-sentinel-alarm)

Scope shipped:
- New module `agents/sentinel/forced_release_alarm.py` — `poll_forced_release_alarms(db_url, last_event_ts)` returns `(list[ForcedReleaseAlarm], new_cursor)`. Read-only on `lease_plane.lease_plane_events` and `lease_plane.deprecated_schemes`.
- Per-event alarm for `event_type='forced'`: one alarm per ad-hoc forced-release event, severity `high` (per RFC §7.10 alarm-on-every-event).
- Batched alarm for `event_type='lease.deprecation_swept'`: groups by `deprecation_id`, emits one summary alarm per completed sweep (only when `deprecated_schemes.sweep_completed_at IS NOT NULL`), severity `medium` (per RFC §7.11.5 batch suppression).
- Cursor state via `Sentinel.load_state()/save_state()` under `forced_release_alarm.last_event_ts` — successive cycles don't re-emit alarms for already-seen events.
- Wired into `Sentinel._run_cycle_inner` via `_emit_forced_release_alarms()` — runs at the start of each cycle; DB unreachable degrades gracefully (logged, swept under the rug, doesn't break the cycle).

### PR 3b — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.10 per-event alarm for ad-hoc `event_type='forced'` | `agents/sentinel/forced_release_alarm.py::_poll_inner` ad-hoc query + `_ad_hoc_alarm` | `test_sentinel_force_release_alarm_per_event` | DONE |
| Cursor-based dedup so successive polls don't re-emit | same, ts-filter on cursor | `test_sentinel_force_release_alarm_dedupes_via_cursor` | DONE |
| §7.11.5 batched alarm for `event_type='lease.deprecation_swept'` | `_poll_inner` batch query + `_batch_alarm` | `test_sentinel_batch_alarm_for_deprecation_sweep` | DONE |
| §7.11.5 batched alarm waits for `sweep_completed_at IS NOT NULL` | same query (JOIN on deprecated_schemes filters partial sweeps) | `test_sentinel_batch_alarm_only_after_sweep_completed_at` | DONE |
| §7.11.7 Phase 0 race window — full integration test | (covered by PR 3a's `deprecate_cmd` serializable tx + advisory lock; this PR adds the test) | `test_phase_zero_acquire_race_blocked` | DONE |
| Sentinel cycle wiring | `agents/sentinel/agent.py::_emit_forced_release_alarms` | (transitively covered — module loads + load_state/save_state + post_finding paths exercised) | DONE |

## PR 4 — `acquire_with_retry` + `_urllib_transport` HTTP-error coverage (this branch: impl/lease-plane-phase-a-pr4-retry-and-409)

Closes the last two RFC §9 Phase A test gates that don't depend on Phase B work.

Scope shipped:
- `LeasePlaneClient.acquire_with_retry()` convenience method (RFC §7.3.3): jittered exponential backoff (floor 100ms, ceiling 5s, full jitter per AWS convention). Honors `retry_after_hint_ms` from the v0.7 §7.3.2 extended `held_by_other` shape as a per-attempt floor. Only `held_by_other` triggers retry; `service_unavailable` / `permission_denied` / `schema_invalid` are terminal.
- `_urllib_transport` HTTP-error body-parse coverage (RFC §7.3.5): closes live-verifier Finding 9 test-coverage gap. The implementation already correctly parses 409 + body, falls back to `permission_denied` on empty 401/403, and `service_unavailable` on empty 5xx — this PR adds tests proving so.

### PR 4 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.3.3 acquire_with_retry returns OK on first attempt | `src/lease_plane/client.py::LeasePlaneClient.acquire_with_retry` | `test_acquire_with_retry_returns_ok_on_first_attempt` | DONE |
| §7.3.3 retry on held_by_other until OK | same | `test_acquire_with_retry_retries_on_held_by_other_until_ok` | DONE |
| §7.3.3 jittered backoff in [100ms, 5s] full jitter | same | `test_acquire_with_retry_jittered_backoff_within_bounds` | DONE |
| §7.3.3 honors retry_after_hint_ms as per-attempt floor | same | `test_acquire_with_retry_honors_retry_after_hint_as_floor` | DONE |
| §7.3.3 service_unavailable terminal (no retry) | same | `test_acquire_with_retry_returns_service_unavailable_without_retry` | DONE |
| §7.3.5 _urllib_transport parses 409 body | `src/lease_plane/client.py::_urllib_transport` HTTPError branch | `test_urllib_transport_parses_409_body` | DONE |
| §7.3.5 _urllib_transport 401 fallback | same | `test_urllib_transport_401_returns_permission_denied_when_no_body` | DONE |
| §7.3.5 _urllib_transport 5xx fallback | same | `test_urllib_transport_500_returns_service_unavailable_when_no_body` | DONE |

## PR 5 — Council BLOCK fixes from PR 1-4 stack review (this branch: impl/lease-plane-phase-a-pr5-council-fixes)

Three-voice council pass on the PR 1-4 cumulative stack (dialectic, code-reviewer, live-verifier; adversarial framing per `feedback_council-adversarial-prompt.md`) surfaced 3 BLOCKs and several CONCERNs/NITs. PR 5 fixes the BLOCKs + the small CONCERNs/NITs; bigger CONCERNs (Phase 2/3 atomicity rewrite, Elixir router canonicalization, Sentinel asyncpg pool) deferred to PR 6+.

### PR 5 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| BLOCK 1 — Elixir 409 emits all 5 v0.7 §7.3.2 fields | `elixir/lease_plane/lib/unitares_lease_plane/{http_router.ex,repo.ex}` | Elixir test "different holder → 409 held_by_other" extended (operator-verified via `mix test`) | DONE |
| BLOCK 1 defense-in-depth — `retry_after_hint_ms` defaults to 0 | `src/lease_plane/models.py::AcquireHeldByOther` | (covered by existing AcquireHeldByOther parse tests + new test below) | DONE |
| BLOCK 2 — CLI advisory lock key deterministic across processes | `scripts/dev/lease_plane_deprecate.py::_lock_key_for_kind` | `test_deprecate_lock_key_stable_across_processes` (subprocess-based) | DONE |
| BLOCK 3 — restore `earned_status` immutability guard | `db/postgres/migrations/029_lease_plane_earned_status_guard.sql` | `test_migration_029_blocks_earned_status_update`, `test_migration_029_still_allows_release_update` | DONE |
| CONCERN — `acquire_with_retry` validates `max_attempts >= 1` and `floor_s <= ceiling_s` | `src/lease_plane/client.py::acquire_with_retry` guards | `test_acquire_with_retry_rejects_max_attempts_below_1`, `test_acquire_with_retry_rejects_floor_exceeding_ceiling` | DONE |
| CONCERN — Sentinel cursor stops advancing to `sweep_completed_at` | `agents/sentinel/forced_release_alarm.py::_poll_inner` | (covered by existing `test_sentinel_force_release_alarm_dedupes_via_cursor`; cursor advance now event-ts only) | DONE |
| NIT — drop redundant inner import in `acquire_with_retry` | `src/lease_plane/client.py::acquire_with_retry` | (no test; cosmetic) | DONE |

Race-window test updated (`tests/test_sentinel_forced_release_alarm.py::test_phase_zero_acquire_race_blocked`) to import `_lock_key_for_kind` instead of computing `abs(hash(kind))` locally — the pre-PR-5 test only passed because both holder and CLI ran in the same Python process; now exercises the same helper as production.

## PR 6 — Deprecation event-vocabulary completeness (this branch: impl/lease-plane-phase-a-pr6-deprecation-events)

Closes the dangling-vocabulary council NIT: migration 027 added `lease.deprecation_marked` and `lease.deprecation_migrated` to the `event_type` CHECK constraint, but no code path emitted them. PR 6 wires both emissions in the CLI, with idempotent guards so re-runs don't double-emit.

### PR 6 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.11.3 Phase 0 emits `lease.deprecation_marked` | `scripts/dev/lease_plane_deprecate.py::deprecate_cmd` | `test_deprecate_emits_lease_deprecation_marked_event` | DONE |
| §7.11.3 Phase 3 emits `lease.deprecation_migrated` | `deprecation_finalize_cmd` | `test_finalize_emits_lease_deprecation_migrated_event` | DONE |
| Idempotent re-runs do NOT double-emit `lease.deprecation_migrated` | finalize early-return on already-finalized | `test_finalize_idempotent_does_not_double_emit` | DONE |

Phase 0 emission uses `INSERT ... RETURNING deprecation_id` so the `ON CONFLICT DO NOTHING` path correctly returns NULL on idempotent re-marks → no double-emit. Phase 3 captures `check_migrated_at` BEFORE the UPDATE and only emits if it was previously NULL.

The marker events use surface_id `f"{kind}:/__deprecation_marker__"` — distinct from any real lease's surface_id, makes them easy to filter in audit queries.

## PR 7 — Elixir router server-side canonicalization + transition_handoff INSERT fix (this branch: impl/lease-plane-phase-a-pr7-elixir-canonicalize)

Closes the council CONCERN named in the prior PR 7+ section: split-brain prevention when non-Python callers (curl, future Hermes/Codex/Elixir clients) hit the lease plane HTTP API directly. Also bundles a one-line surgical fix to `transition_handoff` reacquire INSERT (PR 1 oversight that was masked because migrations 026-029 had never been applied to the live `governance` DB).

**Discovered while running baseline:** migrations 026, 027, 028, 029 existed as files but had never been applied to the live `governance` DB despite being landed in PRs 1, 3a, 5. `core.schema_migrations` only had versions 24, 25. Operator authorized apply at 02:26 local on 2026-05-02; mix tests went from 22/49 failing to 0/49 with the migrations present. The PR 1 `surface_kind`-INSERT-drop fix had been correctly applied to the `acquire` path but missed the `transition_handoff` reacquire path (release-and-reacquire pattern); same RFC gate (§7.2.3) and same root cause, fixed surgically here. KG finding `2026-05-02T08:22:47.029541+00:00` filed for the migration-apply gap.

Scope shipped:
- `elixir/lease_plane/lib/unitares_lease_plane/canonicalize.ex` (new) — Elixir mirror of `src/lease_plane/canonicalize.py` for the four pure-logic schemes (`dialectic`, `resident`, `capture`, `td`). Top-level rejection for NUL, length > PATH_MAX, and `?` (per RFC §7.12.4 OPERATOR_NOTE 3, mirrors Python `_validate_surface_id`). Per-scheme rules audited against Python row-by-row.
- `file://` canonicalization deferred — module emits a `Logger.warning` on every ingress as a deferral audit trail. PR 2.5 migrated all production agents (watcher, vigil, sentinel, chronicler, ship.sh) off `file://` to `resident:/`, verified via `grep`.
- Wired into `http_router.ex::extract_acquire_params` (POST /v1/lease/acquire) and the GET /v1/lease/status query-param handler. On `{:error, reason}` → 422 schema_invalid with reason in body.
- `repo.ex::transition_handoff` reacquire INSERT drops `surface_kind` from the column list (PR 1 oversight).
- `lease_test_helpers.ex::unique_surface_id` switched from `Base.url_encode64` (mixed-case) to `Base.encode16(case: :lower)` so generated test surface_ids are already canonical for `dialectic:/` (lowercase) and don't trip round-trip-mismatch in incidental tests.

Three-voice council pass (dialectic-knowledge-architect + feature-dev:code-reviewer + live-verifier) ran with adversarial framing per `feedback_council-adversarial-prompt.md`. Two BLOCKs surfaced and fixed:
- **Architect B1**: top-level `?` rejection was missing (Elixir only rejected `?` inside `resident:/`; Python rejects at top level). Fixed by adding top-level `?` check with reason `:reserved_query_string`.
- **Reviewer B1**: `~r/[\s?#&]/u` over-rejected — PCRE `\s` matches `\r`/`\f`/`\v` plus Unicode whitespace; Python rejects exactly `(' ', '\t', '\n')`. Fixed by replacing with `~r/[ \t\n#&]/` (exact mirror, no `u` flag).
- **Reviewer B2**: HTTP capture test computed `surface_canonical` by hand in a correct-by-coincidence way; cleanup would target wrong row on assertion failure. Fixed by computing via `Canonicalize.canonicalize/1` directly.
- Live-verifier: 5/5 verifiable claims VERIFIED (migrations applied, grammar CHECK rejects bad scheme, mix test 88 passing pre-fix / 91 post-fix, both surface_leases INSERT paths clean, no production `file://` use, no test-tree base64url remnants).

Plus addressed CONCERNs: file:// `Logger.warning` audit trail (architect C4), `td:/` empty-path parity test (architect C5), byte_size-vs-len divergence doc note (architect C3), capture:/ comma-in-member doc note (architect C6), pattern-match prefix dispatch instead of `binary_part` (architect N2). Two CONCERNs explicitly noted but not coded: capture:/ empty-member-list passthrough (Python agrees, RFC silent — unchanged) and pre-PR-7 row backfill (live-verifier confirmed `surface_leases` is empty, no backfill needed).

### PR 7 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.12.1 server-side canonicalization on Elixir router (closes split-brain from non-Python callers) | `elixir/lease_plane/lib/unitares_lease_plane/canonicalize.ex` (new) | `test/canonicalize_test.exs` (38 unit tests covering scheme dispatch, NUL/length/`?` guards, four implemented schemes, file:// deferral, cross-language parity) | DONE |
| §7.12.4 OPERATOR_NOTE 3 — top-level `?`-rejection (parity with Python `_validate_surface_id`) | `canonicalize.ex::canonicalize/1` top-level guard, reason `:reserved_query_string` | `"rejects ? at top level across ALL schemes (parity with Python _validate_surface_id)"` | DONE |
| §7.12.1 wire canonicalize into POST /v1/lease/acquire | `http_router.ex::extract_acquire_params` (canonicalize before assembling params; on error → 422 schema_invalid) | `test/http_router_test.exs::"PR 7 — non-canonical scheme → 422 schema_invalid"`, `"PR 7 — capture:/ unsorted members → server stores canonical (sorted)"`, `"PR 7 — dialectic:/ uppercase → server stores canonical (lowercased)"` | DONE |
| §7.12.1 wire canonicalize into GET /v1/lease/status | `http_router.ex` status route (canonicalize query param; on error → 422) | `test/http_router_test.exs::"GET /v1/lease/status non-canonical scheme → 422 schema_invalid"` | DONE |
| §7.12.1 step 4 — `file://` realpath/case-fold normalization on Elixir | (deferred — moduledoc + `Logger.warning` on every `file://` ingress as audit trail) | `test/canonicalize_test.exs::"file:// (deferred normalization)"` describe block | DEFERRED — to PR 7.5 (see PR 8+ section below) |
| §7.2.3 `transition_handoff` reacquire INSERT drops `surface_kind` (PR 1 oversight surfaced when migrations 026-029 hit live DB 2026-05-02) | `repo.ex::transition_handoff` insert column list | `test/http_router_test.exs::"accept closes the old lease and reacquires for the recipient"` (turned green from baseline-failing post-migration) | DONE |
| Operator: apply migrations 026-029 to live `governance` DB | `db/postgres/migrations/026|027|028|029_*.sql` (already merged in PRs 1, 3a, 5; never run) | `core.schema_migrations` shows versions 24-29; KG finding `2026-05-02T08:22:47.029541+00:00` | DONE (operator action, 2026-05-02 02:26-02:27 local) |

**Total: 7 rows in PR 7. Within the ≤10 methodology cap.**

## PR 7.5 — `file://` canonicalization in Elixir (this branch: impl/lease-plane-phase-a-pr7-5-elixir-file-canonicalize)

Closes the deferred file:// scheme PR 7 left as a `Logger.warning` stub. Mirrors Python `_canonicalize_file` end-to-end: shell-out to OS `realpath` for symlink resolution and existence-strict mode, double-realpath for the macOS `/var` → `/private/var` idempotency edge (DRIFT-2 in Python), tmpfile probe for case-insensitive FS detection (DRIFT-3 in Python — `pathconf(_PC_CASE_SENSITIVE)` was REFUTED on macOS), `:persistent_term`-cached probe result, lowercase if FS is case-insensitive, trailing-slash strip except for root.

ENOENT (path doesn't exist) handled per RFC §7.12.2 + Python: best-effort canonicalization that resolves intermediate symlinks but appends the missing tail verbatim. Without this, `file:///var/missing/foo` on macOS would canonicalize differently in Elixir (`/var/...`) and Python (`/private/var/...`) — exactly the split-brain class PR 7 + PR 7.5 jointly exist to close.

Three-voice council pass (architect + reviewer + live-verifier) with adversarial framing per `feedback_council-adversarial-prompt.md`. Two BLOCKs surfaced and fixed:

- **BLOCK 1**: dash-prefixed paths could inject GNU realpath flags (`-s` suppresses symlink resolution; `--relative-to=DIR` changes output base). `System.cmd` doesn't go through shell so command injection is safe, but flag injection wasn't. Fixed by prepending `./` to any leading-`-` path before passing to realpath. Also resolves the realpath binary path at compile time via `System.find_executable/1` so a LaunchAgent with sparse PATH gets a load-time error instead of silent `:other` → `:invalid_scheme` failures (architect's framing of B1).
- **BLOCK 2**: ENOENT branch was returning `path` as-given without resolving intermediate symlinks. Added pure-Elixir `nonstrict_realpath/1` helper (walks back to longest-existing prefix, strict-realpaths it, appends missing tail) — mirrors Python's `os.path.realpath(path)` non-strict behavior.

Plus: `LC_ALL=C` in `System.cmd` env (locale-independent error matching), `Logger.warning` on `:other` errors so operators can diagnose what was swallowed, `:symlink_loop` added to `@type reason`, moduledoc notes for compile-time OS detection assumption + single-FS case-fold assumption.

Live-verifier: 9/10 verifiable claims VERIFIED (1 BLOCKED — no deployed Elixir service to curl-test, expected). All implementation assumptions confirmed live: BSD realpath strict-by-default (`/bin/realpath`), `/var` → `/private/var` idempotency, APFS case-insensitive detection works, `System.tmp_dir!()` returns `/var/folders/...` (the DRIFT-2 path the implementation handles).

### PR 7.5 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.12.1 file:// realpath + DRIFT-2 double-realpath | `canonicalize.ex::canonicalize_file/1` + `resolve_realpath/1` | `"resolves an existing file via realpath"`, `"macOS /var resolves to /private/var (DRIFT-2 idempotency)"`, `"follows symlinks to the resolved target"` | DONE |
| §7.12.1 ELOOP detection (strict realpath fails on cycle) | `resolve_realpath/1` stderr → `:symlink_loop` | `"ELOOP (symlink cycle) → :symlink_loop"` | DONE |
| §7.12.2 ENOENT pass-through with intermediate-symlink resolution | `canonicalize_file/1` ENOENT branch + `nonstrict_realpath/1` | `"ENOENT → resolves intermediate symlinks, appends missing tail (PR 7.5 BLOCK 2)"` | DONE |
| §7.12.1 trailing-slash strip + root preservation | `finalize_file/1` | `"trailing / stripped except for root"`, `"root / is preserved (not stripped)"` | DONE |
| §7.12.1 DRIFT-3 case-fold via tmpfile probe (cached) | `case_insensitive_fs?/0` + `detect_case_insensitive_fs/0` + `:persistent_term` | `"case-fold matches the live FS detection"` | DONE |
| §7.12 idempotency for file:// scheme | full chain | `"is idempotent — canonicalize(canonicalize(x)) == canonicalize(x)"` | DONE |
| BLOCK 1 — dash-prefixed path doesn't inject realpath flag | `resolve_realpath/1` `./` prefix guard | `"PR 7.5 BLOCK 1 — leading-`-` path does not get parsed as realpath flag"` | DONE |
| BLOCK 1 — realpath binary resolved at compile time | `@realpath_bin` via `System.find_executable/1` | (load-time assertion via `raise` if not found) | DONE |
| Locale-independence (CONCERN B) | `LC_ALL=C` in `System.cmd` env | (covered transitively by all error-class tests) | DONE |
| Operator visibility into swallowed `:other` errors (CONCERN C3) | `Logger.warning` in `:other` branch | (manual log inspection) | DONE |

**Total: 10 rows in PR 7.5. At the methodology cap.**

## R1 — `deprecate-and-finalize` super-command (this branch: impl/lease-plane-r1-deprecate-supercommand)

Closes the §7.11.2 atomicity residual that PR 5 deferred. Pre-R1 state was a 3-way contradiction shipped under PR 5's "council saw it, deferred it" stamp: (a) RFC v0.8 §7.11.2 line 775 commits to "same operator session" for Phases 2+3 (closing v0.7 dialectic BLOCK-E + code-reviewer BLOCK-3), (b) §9 named gate `test_deprecation_sweep_and_check_migration_atomic_session` codified that commitment, (c) the CLI implementation went multi-step with no cross-invocation session binding, (d) the named test was never written. R1 picks the code-not-discipline side per `feedback_memory-not-guardrail.md` (load-bearing safety in code, tests, type systems — not natural-language warnings).

**Operator decisions captured 2026-05-02:**
1. **Option A1**: super-command + keep singletons as escape-hatches (architect council recommendation)
2. **Two-tx-one-connection**: code-enforces "same operator session" at the DB wire level (single asyncpg connection across both phases) without rolling back successful Phase 2 sweep work if Phase 3 finalize fails
3. **Shared `run_id` (uuid4)** in logs + every event payload so partial completion is operator-visible and rerunnable via audit query (`SELECT ... WHERE payload->>'run_id' = '<uuid>'`)
4. **`lease.deprecation_aborted` event class** added (closes the latent ontology gap architect surfaced — operators who fail Phase 3 thrice and give up now have an audit trail)

Scope shipped:
- `db/postgres/migrations/030_lease_plane_aborted_event.sql` — extends `lease_plane_events_event_type_check` to permit `lease.deprecation_aborted`. Idempotent.
- `scripts/dev/lease_plane_deprecate.py` refactored: `_sweep_inner(conn, *, kind, deprecation_id, run_id)` and `_finalize_inner(conn, *, kind, run_id)` extracted from existing commands; new `deprecate_and_finalize_cmd(*, kind, db_url)` opens one connection, runs both phases in two transactions correlated by run_id; `_emit_aborted_event(conn, *, kind, deprecation_id, run_id, reason)` emits the abort event in its own short tx on Phase 3 failure.
- `_payload_with_run_id(base, run_id)` helper threads run_id into existing event payloads (Phase 0 mark, sweep, migrated). Singletons pass `run_id=None`; field omitted to preserve existing payload shapes.
- Operator runbook (`docs/operations/lease-plane-operator-runbook.md`): canonical sequence + recovery playbook + "any abandoned deprecations?" audit query.
- Singleton sub-commands' `--help` text marked **ESCAPE HATCH — prefer `deprecate-and-finalize`**.

### R1 — RFC gate → code surface → test name → status table

| Gate | Code | Test | Status |
|------|------|------|--------|
| §7.11.2 same-operator-session invariant on a single asyncpg connection across Phase 2+3 | `scripts/dev/lease_plane_deprecate.py::deprecate_and_finalize_cmd` | `test_deprecation_sweep_and_check_migration_atomic_session` (the named §9 gate, finally implementable) | DONE |
| §7.11.2 two-tx-one-connection: Phase 3 failure preserves Phase 2 work + emits aborted event | `deprecate_and_finalize_cmd` Phase 3 try/except → `_emit_aborted_event` | `test_deprecate_and_finalize_phase_3_failure_emits_aborted_event` | DONE |
| §7.11.4 idempotent rerun: standalone `deprecation-finalize` succeeds after super-command Phase 3 failure | `deprecation_finalize_cmd` (now a thin wrapper around `_finalize_inner`) | `test_deprecate_and_finalize_phase_3_failure_then_rerun_finalize_succeeds` | DONE |
| Shared run_id correlates all events from one super-command run | `_payload_with_run_id` + run_id threading through `_sweep_inner` + `_finalize_inner` + `_emit_aborted_event` | `test_deprecate_and_finalize_run_id_correlates_across_events` | DONE |
| §7.11.3 `lease.deprecation_aborted` event class permitted at DB layer | `db/postgres/migrations/030_lease_plane_aborted_event.sql` (idempotency: primary `core.schema_migrations` guard + secondary constraint-text probe) | (covered transitively by the abort-emission test — INSERT would fail without the migration) | DONE |
| Concurrent super-commands on same kind serialize via `pg_try_advisory_lock` | `deprecate_and_finalize_cmd` lock acquisition + rc=4 fail-fast on contention | `test_deprecate_and_finalize_advisory_lock_blocks_concurrent_invocation` | DONE (council CONCERN 3 fix) |
| Abort-emission failure surfaces rc=3 + rerun guidance, not a stack trace | `deprecate_and_finalize_cmd` Phase 3 except branch wraps `_emit_aborted_event` in try/except | `test_deprecate_and_finalize_abort_emission_failure_still_returns_rc_3` | DONE (council BLOCK 1 fix) |
| `sweep_completed_at` preserves first-completion timestamp across re-runs (audit non-drift) | `_sweep_inner` uses `COALESCE(sweep_completed_at, now())` | (covered transitively by existing rerun test — second invocation must not bump sweep_completed_at) | DONE (council CONCERN 4 fix) |
| Operator runbook companion query: SIGKILL-between-phases detection | `docs/operations/lease-plane-operator-runbook.md` "any abandoned deprecations?" — second query for `sweep_completed_at IS NOT NULL AND check_migrated_at IS NULL` | (no test; doc-only) | DONE (council BLOCK 2 fix) |
| §9 RFC named-gate for `lease.deprecation_aborted` test | (deferred to §9 reconciliation residual — see PR 8+ section) | `test_deprecate_and_finalize_phase_3_failure_emits_aborted_event` already exists in this PR; §9 list update is tracked in §9 reconciliation residual scope | DEFERRED |
| Singleton sub-commands clearly marked as escape-hatch in `--help` and module docstring | `_build_parser` help= text + module docstring | (golden output not pinned by test; reviewer checklist) | DONE |
| Operator runbook documents canonical sequence + recovery playbook + abandoned-deprecation audit query | `docs/operations/lease-plane-operator-runbook.md` | (no test; doc-only) | DONE |

**Total: 7 rows in R1. Within the ≤10 methodology cap.**

## PR 8+ — Remaining deferred council CONCERNs and Phase B prerequisites

Bigger council CONCERNs:
- ~~Sentinel asyncpg pool wiring~~ — **CLOSED 2026-05-03**. PR #290 (`9af40fa0`) shipped the reviewer's recommendation: `_poll_sync_forced_release` wraps `asyncio.run(asyncio.wait_for(poll_forced_release_alarms(...), timeout=30s))` inside a `loop.run_in_executor` call from `agents/sentinel/agent.py::_emit_forced_release_alarms`. Production verification 2026-05-03: Sentinel restarted Fri 2026-05-02 19:00 (after PR #290 merged 15:51 same day); >400 cycles since restart with **zero asyncpg/anyio failures** in the forced-release path (the prior "relation does not exist" errors all stopped 2026-05-02 02:22:42, well before restart). The architect's "pool" angle was hygiene-only and is not Phase A blocker.
- ~~§9 RFC test-name reconciliation~~ — **CLOSED 2026-05-03 by PR #304**. Audit progression: baseline 11/4/13 → after #295: 21/1/6 → after #292/#293/#294/#296: 26/1/1 → after #304: **28 exact / 0 variant / 0 missing**. PR #304 added a `# §9: test_deprecation_sweep_uses_forced_release_reason` alias annotation on `test_deprecation_sweep_requires_force_release_token` (variant→exact), and a new `test_force_release_rejects_governance_token` unit test pinning the `_read_force_release_token` choke-point in `scripts/dev/lease_plane_deprecate.py` (missing→exact). Verified: `python3 scripts/dev/audit_rfc_section_9_gates.py` → `28 exact / 0 variant / 0 missing`.
- ~~Sentinel `conflict_held_by_other` alarm rule~~ — **CLOSED 2026-05-03**. Phase A contract layer + dispatch advisory hooks started producing `conflict_held_by_other` events (verified live: 3 events in `lease_plane_events` between 2026-05-02 and 2026-05-03, surface_kind ∈ {file, dialectic}); Sentinel was polling only `forced` and `lease.deprecation_swept`, so these were invisible. Added a third batched-by-surface_id rule (kind=`conflict_batch`, severity=medium, fingerprint includes `last_ts` so successive bursts on the same surface remain distinguishable) to `agents/sentinel/forced_release_alarm.py::_poll_inner`. Cursor logic untouched per PR 5 council fix at lines 130-134. Tests: `test_sentinel_conflict_alarm_batched_per_surface`, `test_sentinel_conflict_alarm_separate_per_surface`, `test_sentinel_conflict_alarm_dedupes_via_cursor` in `tests/test_sentinel_forced_release_alarm.py`.

**Phase A status: COMPLETE as of 2026-05-03.** Both bigger council CONCERNs above are closed; all Phase A PRs (1, 2, 2.5, 3a, 3b, 4, 5, 6, 7, 7.5, R1, audit-script #291, §9 gap-fills #292/#293/#294/#295/#296, contract-layer hardening #297/#299, §9 residual #304) have shipped. The remaining `Phase B prerequisites` below are genuinely non-blocking and intentionally gated on operator-side instrumentation data.

Phase B prerequisites (non-blocking for Phase A):
- ~~Payload-shape standardization pass — commits to writing canonicalized `surface_id` (per §7.12.1) into `audit.tool_usage.payload`, no percent-encoding (per §7.2.8 cross-track).~~ **DONE 2026-05-03 (v0.10):** projection implemented by `UnitaresLeasePlane.Repo.tool_usage_payload/1` (was shipped during Phase A without status promotion); contract pinned by new test at `elixir/lease_plane/test/unitares_lease_plane_test.exs:221`. See RFC §7.2.8 v0.10 for the standardized top-level keys table.
- ~~`unitares_doctor.py` extension to lint that no Elixir source mentions a scheme not in the live grammar CHECK.~~ **DONE 2026-05-03 (v0.10):** `check_elixir_scheme_grammar_lint` in `scripts/dev/unitares_doctor.py:313` (was shipped during Phase A without status promotion). See RFC §7.2.9 v0.10 for the inverse-direction relationship to `check_elixir_deprecated_scheme_lint`.
- ~~§7.5 `remote_heartbeat` instrumentation (operator action — measure Pi↔Mac heartbeat gap distribution ≥7d before any `remote_heartbeat` Phase B promotion).~~ **DONE 2026-05-03 (v0.9):** Pi path resolved by mining 48d of `audit.events WHERE event_type='eisv_sync'` Steward audit trail (n=8452); recommended Pi `original_ttl_s` raised from 180s → 1000s. Mac path remains provisional pending separate measurement of Mac-resident loopback cadence. See RFC §7.5 v0.9 for methodology + numbers.

## Reviewer checklist for any Phase A PR

A Phase A PR is reviewable iff:

- [ ] PR description includes the four-column RFC↔code table for the rows it implements.
- [ ] Every code change in the diff is referenced by exactly one row in the table (or explicitly called out as a refactor / cleanup).
- [ ] Every test added in the diff is named in the table.
- [ ] Out-of-scope rows are marked `(deferred to PR N)` so reviewers don't ask "where's gate X?".
- [ ] If the PR touches a runtime surface listed in `CLAUDE.md` "Before Starting Work on a Single-Writer Surface", the surface-collision check (`gh pr list ... --search`) was run; result included in the description.

## Migration-window and ship-order constraints

- Migration 026 must ship before any caller starts populating `surface_leases`. Live-verifier confirmed the table is empty as of v0.8 council pass; this remains true iff no Phase A code lands between v0.8 RFC freeze and PR 1 merge. Do not start any other lease-plane code work in parallel.
- Migration 027 ships in the same PR as 026 to avoid a window where `surface_kind_catalog` doesn't exist but PR 3's CLI tries to FK against it.
- The v0.7 drift fix to `http_router.ex` MUST land in the same merge as migration 026 — partial deploy where 026 lands but the router still requires `surface_kind` from the body causes hard runtime failure on every acquire.

## Open questions for operator before PR 1 starts

1. **CLI ergonomics for `lease-plane deprecate ...`** — Mix task in the Elixir app, or standalone Python CLI in `scripts/dev/`? (Defer to PR 3, but flag now so the CLI doesn't surprise.)
2. **Sentinel polling interval for the new alarm rule** — current Sentinel has its own cadence; should the lease-plane events poll be more aggressive, given §7.10's "rare events" framing? (Default: same cadence as existing Sentinel rules.)
3. **Migration 026 pre-flight check** — abort with error if `surface_leases` is non-empty (safe; matches v0.8 RFC §7.2.3 caveat), or proceed with DROP COLUMN regardless (faster but risks data loss)? (Default: abort if non-empty.)
