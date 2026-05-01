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

## PR 2.5 — Production-agent surface_id migration + AcquireRequest field_validator (planned, not yet drafted)

**Surfaced during PR 2 implementation:** the production agents (watcher, vigil, sentinel, chronicler, ship.sh advisory) use surface_ids like `watcher:scan_commits:<repo>`, `vigil:cycle`, `sentinel:cycle`, `chronicler:scrape`, `ship.sh:test-branch` — none of which match the canonical scheme list (RFC §7.2.1). Wiring the AcquireRequest field_validator now would cause every agent to crash on first acquire with `ValidationError: scheme not in canonical scheme list`.

Scope:
- Migrate production surface_ids to canonical schemes:
  - `watcher:scan_commits:X` → `resident:/watcher_scan_commits_X`
  - `vigil:cycle` → `resident:/vigil_cycle`
  - `sentinel:cycle` → `resident:/sentinel_cycle`
  - `chronicler:scrape` → `resident:/chronicler_scrape`
  - `ship.sh:<branch>` → `resident:/ship_sh_<branch>` (operator-driven, not file-bound)
- After migration, wire the AcquireRequest `field_validator` on `surface_id` per §7.12.5 — auto-canonicalize at the model boundary; reject NUL bytes, `?`-bearing strings, and non-canonical schemes.
- Un-skip the 2 deferred test gates: `test_acquire_request_rejects_query_string_in_surface_id` and `test_acquire_request_surface_id_field_validator_wired`.
- Update `lease_advisory_scope()` to drop `surface_kind` parameter entirely (currently kept as optional+ignored).

Rationale for splitting from PR 2: the canonicalize.py helper is reviewable independently; coupling it to a fleet-wide surface_id migration risks both reviews stalling on the wider impact. PR 2 ships the helper; PR 2.5 ships the migration + validator together (they MUST land atomically — validator without migration = bricked fleet; migration without validator = silently inconsistent).

## PR 3 — §7.11 deprecation CLI + Sentinel forced-release alarm (planned, not yet drafted)

Scope (anticipated):
- `lease-plane` CLI commands: `deprecate`, `deprecation-sweep`, `deprecation-finalize` (standalone Python CLI in `scripts/dev/`, per operator decision 2026-04-30).
- `LEASE_FORCE_RELEASE_TOKEN` integration test (covers §7.10 + §7.11 force-release wiring).
- Phase 2 sweep job with idempotent predicate.
- Phase 0 race-window mitigation (serializable-tx + advisory lock).
- **Sentinel forced-release alarm rule** (deferred from PR 1): adds direct lease_plane_events DB access to Sentinel; per-event alarm for ad-hoc `event_type='forced'`, batched-by-`deprecation_id` for `event_type='lease.deprecation_swept'`. Default Sentinel cadence per operator decision 2026-04-30.
- 6 §7.11 Phase A test gates + 1 §7.10 force-release test + 1 Sentinel alarm wiring test.

Rationale: depends on PR 1 (catalog + deprecated_schemes tables must exist) and on operator-runbook content for the CLI documentation. Lands after both.

## PR 4+ — Remaining gates

- §7.3.3 `acquire_with_retry()` jittered backoff method
- §7.3.5 HTTP 409 body-parse test (`test_urllib_transport_parses_409_body`)
- Phase B prerequisites (payload-shape standardization spec, `unitares_doctor` extension)

These are mostly self-contained; pick up after PR 1-3 land.

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
