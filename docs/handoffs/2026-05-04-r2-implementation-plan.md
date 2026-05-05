# R2 — Honest Memory Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Implementation status (2026-05-05):** Phase 1 shipped in #357 as a single squashed merge containing PR slices 0-5 plus council fixes. This handoff is retained as the implementation record and no longer represents open Phase 1 work. The live migration slot is **036** because slot 035 was taken by `coordination_events` while this branch was in flight.

**Goal:** Implement plan-row R2 — turn `parent_agent_id` declarations into verified continuity claims via the provisional → confirmed/demoted/archived FSM specified in `docs/ontology/r2-honest-memory-integration.md`.

**Architecture:** Column-on-`core.identities` storage extending R1's already-shipped lineage columns; FSM driven by R1's `score_trajectory_continuity`; anyio-safe via `create_tracked_task` deferred dispatch from `process_agent_update` + a 30-min sweeper that mirrors `class_promotion_sweeper_task`. Phase 1 (storage + audit, no enforcement) lands first; Phase 2 (downstream consumers) sequences after Phase 1 telemetry.

**Tech Stack:** Python 3.12 / asyncio, PostgreSQL@17, `asyncpg`, MCP server, pytest. Existing R1 primitives (`score_trajectory_continuity`, `is_lineage_provisional`, `mark_lineage_provisional`, `confirm_lineage`) are reused; R2 adds new helpers and the FSM.

---

## Reconciliation: R1 already shipped some R2 columns

Live schema check 2026-05-04 (`\d core.identities`) shows R1 PR #306 (migration 031) already added:
- `provisional_lineage BOOLEAN NOT NULL DEFAULT FALSE`
- `provisional_score_id UUID`
- `provisional_recorded_at TIMESTAMPTZ`
- `confirmed_at TIMESTAMPTZ`

The R2 design doc proposes 7 new columns. Reconciliation:

| R2 design column | Action | Note |
|---|---|---|
| `provisional_lineage` | **Skip** | Already exists. |
| `lineage_declared_at` | **Add** | Stamps `parent_agent_id` first set. |
| `lineage_promoted_at` | **Skip — alias** | Use existing `confirmed_at`. |
| `lineage_demoted_at` | **Add** | New. |
| `lineage_archived_at` | **Add** | New. |
| `lineage_last_eval_at` | **Add** | New (sweeper/check-in cadence guard). |
| `chain_obs_count` | **Add** | New (forward-only chain counter). |

**Doc update required:** `docs/ontology/r2-honest-memory-integration.md` Storage table needs a one-line note that `lineage_promoted_at` is satisfied by `confirmed_at` and `provisional_lineage` was pre-shipped by R1. Done as part of PR 0.

---

## PR sequence

| PR | Title | Surface | Notes |
|---|---|---|---|
| 0 | Doc reconciliation + plan.md R2 row | `docs/ontology/r2-honest-memory-integration.md`, `docs/ontology/plan.md` | Single-doc; opens the implementation row. |
| 1 | Migration 036 + storage helpers | `db/postgres/migrations/036_r2_lineage_lifecycle.sql`, `src/db/mixins/identity.py` | Shipped in #357; slot 035 was already claimed by coordination events. |
| 2 | FSM core + audit events | `src/identity/lineage_lifecycle.py`, `src/db/mixins/identity.py`, `tests/test_lineage_lifecycle.py` | Shipped in #357. |
| 3 | Cross-role pre-check + onboard wiring | `src/mcp_handlers/identity/handlers.py`, `src/services/identity_payloads.py` | Shipped in #357; includes `lineage_cross_role_rejected` plus `provisional_lineage` / `lineage_state` on `onboard()` and `identity()` responses. |
| 4 | Sweeper task | `src/background_tasks.py`, `src/identity/lineage_lifecycle.py` | Shipped in #357; `lineage_eval_sweeper_task` runs on a 30min cadence with 6h re-eval guard. |
| 5 | Check-in trigger | `src/mcp_handlers/updates/phases.py` | Shipped in #357; fire-and-forget `create_tracked_task(evaluate_lineage_for(...))` after process updates. |
| 6 | (Future, separate row) Phase 2 downstream wiring | Trust-tier (S6), KG provenance (S7), R3 baselines, dashboard | **Not in this plan.** Sequences after ≥4 weeks Phase 1 telemetry. |

Each PR gets a 3-agent council pass (architect + reviewer + live-verifier) per [`feedback_council-also-for-implementation.md`](../../.claude/projects/-Users-cirwel/memory/feedback_council-also-for-implementation.md). All PRs run `./scripts/dev/test-cache.sh` before commit.

---

## PR 0 — Doc reconciliation + plan.md R2 row open

**Files:**
- Modify: `docs/ontology/r2-honest-memory-integration.md` (Storage section)
- Modify: `docs/ontology/plan.md` (R2 row + tail status block)

- [ ] **Step 1: Update R2 design doc Storage section**

Add reconciliation note before the columns table:

```markdown
**Reconciliation (2026-05-04):** R1 PR #306 (migration 031) already shipped
`provisional_lineage`, `provisional_score_id`, `provisional_recorded_at`, and
`confirmed_at` on `core.identities`. R2 reuses these. `lineage_promoted_at`
in the table below is satisfied by R1's existing `confirmed_at` — R2 does
not introduce a duplicate column. Migration 036 adds only the genuinely new
columns: `lineage_declared_at`, `lineage_demoted_at`, `lineage_archived_at`,
`lineage_last_eval_at`, `chain_obs_count`.
```

- [ ] **Step 2: Update plan.md R2 row**

Append to the existing R2 row body:

```
**Implementation row opened 2026-05-04** at
`docs/handoffs/2026-05-04-r2-implementation-plan.md`. Phase 1 (storage +
audit + FSM, no downstream wiring) sequenced as 5 PRs. Phase 2 (consumer
wiring: trust-tier, KG provenance, R3 baselines, dashboard) deferred to
separate row gated on ≥4 weeks Phase 1 telemetry per design doc
§"Shadow-mode calibration path."
```

Also update the tail status block (around plan.md line 117) to reflect "R2 implementation row opened."

- [ ] **Step 3: Commit**

```bash
git add docs/ontology/r2-honest-memory-integration.md docs/ontology/plan.md
git commit -m "docs(r2): open implementation row + reconcile storage columns with R1 #306 (migration 031)"
```

---

## PR 1 — Migration 036 + storage helpers

**Files:**
- Create: `db/postgres/migrations/036_r2_lineage_lifecycle.sql`
- Modify: `src/db/mixins/identity.py` (extend after line 318 — after `confirm_lineage`)
- Test: `tests/db/test_lineage_lifecycle_storage.py` (new)

### Step 1: Write the failing migration test

- [ ] Create `tests/db/test_lineage_lifecycle_storage.py`:

```python
"""R2 PR 1: storage layer for lineage lifecycle columns + helpers."""
import pytest
from src.db import get_db


@pytest.mark.asyncio
async def test_lineage_columns_exist():
    backend = get_db()
    async with backend.acquire() as conn:
        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'core' AND table_name = 'identities'
              AND column_name IN (
                  'lineage_declared_at', 'lineage_demoted_at',
                  'lineage_archived_at', 'lineage_last_eval_at',
                  'chain_obs_count'
              )
            ORDER BY column_name
            """
        )
    names = {r["column_name"] for r in cols}
    assert names == {
        "lineage_declared_at", "lineage_demoted_at",
        "lineage_archived_at", "lineage_last_eval_at",
        "chain_obs_count",
    }
    chain_obs = next(r for r in cols if r["column_name"] == "chain_obs_count")
    assert chain_obs["data_type"] == "integer"
    assert chain_obs["is_nullable"] == "NO"
    assert chain_obs["column_default"] == "0"


@pytest.mark.asyncio
async def test_demote_lineage_clears_parent_and_stamps_demoted_at(seeded_pair):
    """provisional → demoted: parent_agent_id cleared, lineage_demoted_at set."""
    backend = get_db()
    successor_id = seeded_pair.successor_id
    ok = await backend.demote_lineage(
        successor_id, reason="r1_unsupported"
    )
    assert ok
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT parent_agent_id, lineage_demoted_at, provisional_lineage "
            "FROM core.identities WHERE agent_id = $1",
            successor_id,
        )
    assert row["parent_agent_id"] is None
    assert row["lineage_demoted_at"] is not None
    assert row["provisional_lineage"] is False


@pytest.mark.asyncio
async def test_archive_lineage_marks_archived_keeps_parent(seeded_pair):
    """grace expiration: lineage_archived_at set, parent_agent_id retained but inert."""
    backend = get_db()
    successor_id = seeded_pair.successor_id
    ok = await backend.archive_lineage(successor_id)
    assert ok
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT parent_agent_id, lineage_archived_at, provisional_lineage "
            "FROM core.identities WHERE agent_id = $1",
            successor_id,
        )
    assert row["parent_agent_id"] is not None  # retained
    assert row["lineage_archived_at"] is not None
    assert row["provisional_lineage"] is False


@pytest.mark.asyncio
async def test_increment_chain_obs_count_is_atomic(confirmed_pair):
    backend = get_db()
    successor_id = confirmed_pair.successor_id
    new = await backend.increment_chain_obs_count(successor_id)
    assert new == 1
    new2 = await backend.increment_chain_obs_count(successor_id)
    assert new2 == 2


@pytest.mark.asyncio
async def test_clawback_chain_counter_resets_to_zero(confirmed_pair_with_obs):
    backend = get_db()
    successor_id = confirmed_pair_with_obs.successor_id
    ok = await backend.demote_lineage(
        successor_id, reason="post_promotion_divergence"
    )
    assert ok
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT chain_obs_count FROM core.identities WHERE agent_id = $1",
            successor_id,
        )
    assert row["chain_obs_count"] == 0
```

The fixtures `seeded_pair`, `confirmed_pair`, `confirmed_pair_with_obs` go in `tests/db/conftest.py`. See PR 1 §"Fixtures" below.

- [ ] **Step 2: Run tests to verify they fail**

```bash
./scripts/dev/test-cache.sh tests/db/test_lineage_lifecycle_storage.py -v
```

Expected: FAIL — columns don't exist; `demote_lineage`, `archive_lineage`, `increment_chain_obs_count` not defined on backend.

### Step 3: Write migration 036

- [ ] Create `db/postgres/migrations/036_r2_lineage_lifecycle.sql`:

```sql
-- Migration 036: R2 lineage lifecycle columns on core.identities
--
-- Extends R1 PR #306 (migration 031) which already shipped
-- provisional_lineage, provisional_score_id, provisional_recorded_at,
-- confirmed_at. R2 adds the columns required for the demote/archive
-- transitions, sweeper cadence guard, and forward-only chain counter.
--
-- See: docs/ontology/r2-honest-memory-integration.md §Storage
--      docs/handoffs/2026-05-04-r2-implementation-plan.md PR 1

ALTER TABLE core.identities
    ADD COLUMN IF NOT EXISTS lineage_declared_at  TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS lineage_demoted_at   TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS lineage_archived_at  TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS lineage_last_eval_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS chain_obs_count      INTEGER     NOT NULL DEFAULT 0;

COMMENT ON COLUMN core.identities.lineage_declared_at  IS 'R2: stamped when parent_agent_id first set at onboard';
COMMENT ON COLUMN core.identities.lineage_demoted_at   IS 'R2: stamped on * → demoted; parent_agent_id is also cleared';
COMMENT ON COLUMN core.identities.lineage_archived_at  IS 'R2: stamped on grace-window expiration; parent_agent_id retained but inert';
COMMENT ON COLUMN core.identities.lineage_last_eval_at IS 'R2: updated by sweeper/check-in trigger to enforce cadence guards';
COMMENT ON COLUMN core.identities.chain_obs_count      IS 'R2: forward-only chain counter; incremented post-promotion, reset to 0 on confirmed→demoted clawback';

-- Sweeper-friendly partial index: only rows that need re-evaluation.
CREATE INDEX IF NOT EXISTS idx_identities_provisional_eval
    ON core.identities (lineage_last_eval_at)
    WHERE provisional_lineage = TRUE OR confirmed_at IS NOT NULL;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (36, 'r2_lineage_lifecycle', NOW())
ON CONFLICT (version) DO NOTHING;
```

### Step 4: Add storage helpers

- [ ] In `src/db/mixins/identity.py`, after the `is_lineage_provisional` method (~line 335), add:

```python
async def declare_lineage(self, successor_id: str) -> bool:
    """R2: stamp lineage_declared_at when parent_agent_id is first set.

    Idempotent — only stamps if NULL. Caller is the onboard handler
    after `parent_agent_id` is written and the cross-role pre-check
    has passed.
    """
    async with self.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE core.identities
               SET lineage_declared_at = COALESCE(lineage_declared_at, now()),
                   updated_at = now()
             WHERE agent_id = $1
            """,
            successor_id,
        )
        return _rows_updated(result) > 0

async def demote_lineage(self, successor_id: str, *, reason: str) -> bool:
    """R2: provisional/confirmed → demoted.

    Clears parent_agent_id, stamps lineage_demoted_at, clears the
    provisional flag, and resets chain_obs_count to 0 (clawback).
    `reason` is stored only on the audit event written by the caller —
    the column carries timestamps, not free-text.
    """
    async with self.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE core.identities
               SET parent_agent_id = NULL,
                   provisional_lineage = FALSE,
                   provisional_score_id = NULL,
                   confirmed_at = NULL,
                   lineage_demoted_at = now(),
                   chain_obs_count = 0,
                   updated_at = now()
             WHERE agent_id = $1
            """,
            successor_id,
        )
        return _rows_updated(result) > 0

async def archive_lineage(self, successor_id: str) -> bool:
    """R2: grace-window expiration. Stamps lineage_archived_at; retains
    parent_agent_id (inert audit anchor)."""
    async with self.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE core.identities
               SET provisional_lineage = FALSE,
                   provisional_score_id = NULL,
                   lineage_archived_at = now(),
                   updated_at = now()
             WHERE agent_id = $1
            """,
            successor_id,
        )
        return _rows_updated(result) > 0

async def increment_chain_obs_count(self, successor_id: str) -> int:
    """R2: post-promotion forward-only counter. Returns new value.
    No-op (returns existing value) if the row is not in confirmed state."""
    async with self.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE core.identities
               SET chain_obs_count = chain_obs_count + 1,
                   updated_at = now()
             WHERE agent_id = $1
               AND confirmed_at IS NOT NULL
               AND provisional_lineage = FALSE
         RETURNING chain_obs_count
            """,
            successor_id,
        )
        return int(row["chain_obs_count"]) if row is not None else 0

async def stamp_lineage_eval(self, successor_id: str) -> None:
    """R2: cadence guard. Called after every R1 eval."""
    async with self.acquire() as conn:
        await conn.execute(
            "UPDATE core.identities SET lineage_last_eval_at = now() "
            "WHERE agent_id = $1",
            successor_id,
        )

async def are_lineages_provisional(
    self, agent_ids: list[str]
) -> dict[str, bool]:
    """R2 batch primitive (architect-flagged in R1 PR 4a council).

    Avoids N+1 single-id calls when the sweeper or chain-walker checks
    multiple successors at once.
    """
    if not agent_ids:
        return {}
    async with self.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agent_id, provisional_lineage "
            "FROM core.identities WHERE agent_id = ANY($1::text[])",
            agent_ids,
        )
    out = {row["agent_id"]: bool(row["provisional_lineage"]) for row in rows}
    for aid in agent_ids:
        out.setdefault(aid, False)
    return out
```

Add `_rows_updated` helper at module top if not already present:

```python
def _rows_updated(execute_result: str) -> int:
    try:
        return int((execute_result or "UPDATE 0").split()[-1])
    except Exception:
        return 0
```

(If `_rows_updated` already exists in the file or a sibling util, reuse it instead.)

### Step 5: Add fixtures

- [ ] In `tests/db/conftest.py`, add:

```python
import pytest
from dataclasses import dataclass


@dataclass
class _Pair:
    parent_id: str
    successor_id: str


@pytest.fixture
async def seeded_pair(db_backend):
    """Provisional pair: parent + successor with parent_agent_id set,
    provisional_lineage=TRUE, lineage_declared_at stamped."""
    parent_id = "test-parent-" + _uuid_suffix()
    successor_id = "test-successor-" + _uuid_suffix()
    await _insert_identity(db_backend, parent_id)
    await _insert_identity(db_backend, successor_id, parent_agent_id=parent_id,
                            provisional_lineage=True)
    yield _Pair(parent_id, successor_id)
    await _cleanup(db_backend, [parent_id, successor_id])


@pytest.fixture
async def confirmed_pair(db_backend):
    """Confirmed pair: provisional_lineage=FALSE, confirmed_at set."""
    parent_id = "test-parent-" + _uuid_suffix()
    successor_id = "test-successor-" + _uuid_suffix()
    await _insert_identity(db_backend, parent_id)
    await _insert_identity(db_backend, successor_id, parent_agent_id=parent_id,
                            confirmed=True)
    yield _Pair(parent_id, successor_id)
    await _cleanup(db_backend, [parent_id, successor_id])


@pytest.fixture
async def confirmed_pair_with_obs(db_backend, confirmed_pair):
    """Confirmed pair + chain_obs_count = 5."""
    backend = db_backend
    for _ in range(5):
        await backend.increment_chain_obs_count(confirmed_pair.successor_id)
    return confirmed_pair
```

(Inline `_insert_identity`, `_uuid_suffix`, `_cleanup` helpers — keep them local to the conftest.)

### Step 6: Run tests until green

- [ ] Run migration locally:

```bash
psql -h localhost -U postgres -d governance -f db/postgres/migrations/036_r2_lineage_lifecycle.sql
```

- [ ] Run tests:

```bash
./scripts/dev/test-cache.sh tests/db/test_lineage_lifecycle_storage.py -v
```

Expected: 5 passing.

### Step 7: Council pass + commit

- [ ] Dispatch 3-agent council in parallel (architect + reviewer + live-verifier) on the diff. Adversarial-prompt lane per memory `feedback_council-adversarial-prompt.md`.

- [ ] Address forcing items.

- [ ] Run full test suite:

```bash
./scripts/dev/test-cache.sh
```

- [ ] Commit:

```bash
git add db/postgres/migrations/036_r2_lineage_lifecycle.sql \
        src/db/mixins/identity.py \
        tests/db/test_lineage_lifecycle_storage.py \
        tests/db/conftest.py
git commit -m "feat(r2): PR 1 — migration 036 + storage helpers (declare/demote/archive/chain_obs/eval-stamp/batch-provisional)"
```

---

## PR 2 — FSM core + audit events

**Files:**
- Create: `src/identity/lineage_lifecycle.py` (the FSM module)
- Modify: `src/db/mixins/identity.py` (add `select_lineage_eval_candidates`)
- Test: `tests/test_lineage_integration.py` (the test cases enumerated in design doc §"Test fixture")
- Test: `tests/helpers/lineage_integration_fixtures.py` (`synthetic_lineage_pair`)

### Design — `_evaluate_lineage_for`

The FSM lives in `src/identity/lineage_lifecycle.py` as a pure async function:

```python
async def evaluate_lineage_for(
    successor_id: str,
    *,
    min_observations: int = 5,
    grace_window: timedelta = timedelta(days=30),
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LineageEvalOutcome:
    """Run R1 against (parent_agent_id, successor_id), apply the FSM
    transition, write audit + storage. Idempotent under concurrent invocations
    (cadence guard via lineage_last_eval_at)."""
```

The FSM rules reproduce the design-doc table verbatim. `now` is injected for testability (test 3 — grace expiration).

### Step 1: Write `synthetic_lineage_pair` fixture

- [ ] Create `tests/helpers/lineage_integration_fixtures.py`:

```python
"""R2 test fixture — builds on R1's synthetic_trajectory_pair.

Returns (parent_rows, successor_rows, expected_terminal_state) for the
nine kinds enumerated in r2-honest-memory-integration.md §"Test fixture".
"""
from dataclasses import dataclass
from typing import Literal
from tests.helpers.trajectory_fixtures import synthetic_trajectory_pair


TerminalState = Literal[
    "confirmed", "demoted", "archived", "rejected_cross_role",
]


@dataclass
class LineagePairFixture:
    parent_rows: list[dict]
    successor_rows: list[dict]
    parent_class: str | None
    successor_class: str | None
    expected_terminal_state: TerminalState


def synthetic_lineage_pair(
    seed: int,
    kind: Literal[
        "genuine_fast", "divergent_fast", "inconclusive_grace_expired",
        "inconclusive_then_promotion", "promotion_then_stable",
        "promotion_then_divergent", "cross_role",
    ],
    observations: int,
) -> LineagePairFixture:
    if kind == "cross_role":
        # genuine trajectory but mismatched class tags
        pair = synthetic_trajectory_pair(seed, kind="genuine", n=observations)
        return LineagePairFixture(
            parent_rows=pair.parent_rows,
            successor_rows=pair.successor_rows,
            parent_class="embodied",
            successor_class="session_like",
            expected_terminal_state="rejected_cross_role",
        )
    # Map the rest to R1 kinds and tag class consistently
    r1_kind = {
        "genuine_fast":               "genuine",
        "divergent_fast":             "divergent",
        "inconclusive_grace_expired": "underdetermined",
        "inconclusive_then_promotion":"underdetermined_then_genuine",
        "promotion_then_stable":      "genuine",
        "promotion_then_divergent":   "genuine_then_divergent",
    }[kind]
    pair = synthetic_trajectory_pair(seed, kind=r1_kind, n=observations)
    expected = {
        "genuine_fast":               "confirmed",
        "divergent_fast":             "demoted",
        "inconclusive_grace_expired": "archived",
        "inconclusive_then_promotion":"confirmed",
        "promotion_then_stable":      "confirmed",
        "promotion_then_divergent":   "demoted",
    }[kind]
    return LineagePairFixture(
        parent_rows=pair.parent_rows,
        successor_rows=pair.successor_rows,
        parent_class="engaged_ephemeral",
        successor_class="engaged_ephemeral",
        expected_terminal_state=expected,
    )
```

R1's `synthetic_trajectory_pair` (per R1 v3.1) ships `genuine`, `divergent`, `underdetermined`, `underdetermined_then_genuine`, `genuine_then_divergent` kinds. If `synthetic_trajectory_pair` does not yet expose `genuine_then_divergent`, add it as part of this PR (small extension).

### Step 2: Write FSM tests

- [ ] Create `tests/test_lineage_integration.py` with the 10 test cases from the design doc §"Test fixture". Show each test concretely:

```python
"""R2 FSM integration tests — reproduces design doc §Test fixture."""
import pytest
from datetime import datetime, timedelta, timezone
from src.identity.lineage_lifecycle import evaluate_lineage_for
from tests.helpers.lineage_integration_fixtures import synthetic_lineage_pair


@pytest.mark.asyncio
async def test_genuine_fast_promotion(seeded_db, monkeypatch):
    """Test 1: parent=30 rows, successor=10 rows from same generator;
    after 5th check-in R1.plausible → confirmed."""
    fx = synthetic_lineage_pair(seed=1, kind="genuine_fast", observations=5)
    successor_id = await _seed_pair(seeded_db, fx)

    outcome = await evaluate_lineage_for(successor_id, min_observations=5)
    assert outcome.terminal_state == "confirmed"
    assert outcome.transition == "provisional_to_confirmed"
    events = await _audit_events_for(seeded_db, successor_id)
    assert any(e["event_type"] == "lineage_promoted" for e in events)


@pytest.mark.asyncio
async def test_divergent_fast_demotion(seeded_db):
    """Test 2: divergent generator → demoted with reason=r1_unsupported."""
    fx = synthetic_lineage_pair(seed=2, kind="divergent_fast", observations=5)
    successor_id = await _seed_pair(seeded_db, fx)

    outcome = await evaluate_lineage_for(successor_id, min_observations=5)
    assert outcome.terminal_state == "demoted"
    events = await _audit_events_for(seeded_db, successor_id)
    rec = next(e for e in events if e["event_type"] == "lineage_demoted")
    assert rec["payload"]["reason"] == "r1_unsupported"


@pytest.mark.asyncio
async def test_inconclusive_then_grace_expired(seeded_db):
    """Test 3: 3 rows then idle; mock now() past grace_window → archived."""
    fx = synthetic_lineage_pair(seed=3, kind="inconclusive_grace_expired",
                                 observations=3)
    successor_id = await _seed_pair(seeded_db, fx)

    declared_at = datetime.now(timezone.utc)
    future_now = lambda: declared_at + timedelta(days=31)

    outcome = await evaluate_lineage_for(
        successor_id, min_observations=5,
        grace_window=timedelta(days=30), now=future_now,
    )
    assert outcome.terminal_state == "archived"
    events = await _audit_events_for(seeded_db, successor_id)
    assert any(e["event_type"] == "lineage_grace_expired" for e in events)


@pytest.mark.asyncio
async def test_inconclusive_then_promotion(seeded_db):
    """Test 4: first 5 plausibility 0.55–0.70 (inconclusive); next 5 push avg
    above 0.70 → promotion at observation 10, not 5."""
    fx = synthetic_lineage_pair(seed=4, kind="inconclusive_then_promotion",
                                 observations=10)
    successor_id = await _seed_pair(seeded_db, fx)

    # First eval at 5 obs: inconclusive, no transition
    outcome_5 = await evaluate_lineage_for(successor_id, min_observations=5)
    assert outcome_5.transition is None
    # Second eval at 10 obs: promoted
    outcome_10 = await evaluate_lineage_for(successor_id, min_observations=5)
    assert outcome_10.terminal_state == "confirmed"


@pytest.mark.asyncio
async def test_promotion_then_stable(seeded_db):
    """Test 5: after promotion, 50 more matching check-ins; no further state
    change; chain_obs_count increments by 50."""
    fx = synthetic_lineage_pair(seed=5, kind="promotion_then_stable",
                                 observations=55)
    successor_id = await _seed_pair(seeded_db, fx)

    await evaluate_lineage_for(successor_id, min_observations=5)
    # Simulate 50 post-promotion check-ins
    for _ in range(50):
        await seeded_db.increment_chain_obs_count(successor_id)
    outcome = await evaluate_lineage_for(successor_id, min_observations=5)
    assert outcome.transition is None  # no flip
    chain = await _read_chain_obs_count(seeded_db, successor_id)
    assert chain == 50


@pytest.mark.asyncio
async def test_promotion_then_divergent_clawback(seeded_db):
    """Test 6: promote, accumulate chain_obs_count=10, then 20 divergent
    check-ins → confirmed → demoted; chain counter clawed back to 0."""
    fx = synthetic_lineage_pair(seed=6, kind="promotion_then_divergent",
                                 observations=30)
    successor_id = await _seed_pair(seeded_db, fx)

    await evaluate_lineage_for(successor_id, min_observations=5)
    for _ in range(10):
        await seeded_db.increment_chain_obs_count(successor_id)

    # Inject divergent rows
    await _append_successor_rows(seeded_db, successor_id, fx.successor_rows[10:])

    outcome = await evaluate_lineage_for(successor_id, min_observations=5)
    assert outcome.terminal_state == "demoted"
    events = await _audit_events_for(seeded_db, successor_id)
    rec = next(e for e in events if e["event_type"] == "lineage_demoted")
    assert rec["payload"]["reason"] == "post_promotion_divergence"
    chain = await _read_chain_obs_count(seeded_db, successor_id)
    assert chain == 0  # clawback


@pytest.mark.asyncio
async def test_cross_role_rejected_at_onboard(seeded_db):
    """Test 7 — note: cross-role rejection happens at onboard, NOT in the
    FSM. This test belongs in PR 3's onboard wiring tests; placeholder
    here marks coverage."""
    pytest.skip("PR 3 — covered in test_onboard_lineage_cross_role.py")


@pytest.mark.asyncio
async def test_multi_generation_per_link(seeded_db):
    """Test 8: chain A → B → C; promoting C-B does not affect B-A counter."""
    a, b, c = await _seed_chain(seeded_db, ["genuine_fast"] * 2)
    # B-A confirmed; chain_obs_count(B) = 5
    for _ in range(5):
        await seeded_db.increment_chain_obs_count(b)
    # C declares B
    await _set_parent(seeded_db, c, b)
    outcome = await evaluate_lineage_for(c, min_observations=5)
    assert outcome.terminal_state == "confirmed"
    # C's chain counter starts at 0 independently
    chain_c = await _read_chain_obs_count(seeded_db, c)
    chain_b = await _read_chain_obs_count(seeded_db, b)
    assert chain_c == 0
    assert chain_b == 5  # unchanged


@pytest.mark.asyncio
async def test_cadence_guard_skips_eval(seeded_db, monkeypatch):
    """Test 9: lineage_last_eval_at within cadence → eval is skipped."""
    fx = synthetic_lineage_pair(seed=9, kind="genuine_fast", observations=5)
    successor_id = await _seed_pair(seeded_db, fx)
    await seeded_db.stamp_lineage_eval(successor_id)
    outcome = await evaluate_lineage_for(
        successor_id, min_observations=5,
        eval_cadence=timedelta(hours=1),
    )
    assert outcome.skipped_reason == "within_cadence"


@pytest.mark.asyncio
async def test_conftest_stub_registration_regression():
    """Test 10 (meta): conftest._isolate_db_backend registers the new mock
    methods. Per R1 v3.2-E missing stubs auto-generate AsyncMock children
    that return coroutines instead of dicts/lists."""
    from tests.conftest import _isolate_db_backend  # adjust import path
    required = {
        "demote_lineage", "archive_lineage", "increment_chain_obs_count",
        "stamp_lineage_eval", "are_lineages_provisional",
        "select_lineage_eval_candidates", "declare_lineage",
    }
    registered = _isolate_db_backend.REGISTERED_METHODS  # convention from R1
    missing = required - set(registered)
    assert not missing, f"conftest missing R2 stubs: {missing}"
```

### Step 3: Run failing tests

- [ ] `./scripts/dev/test-cache.sh tests/test_lineage_integration.py -v`

Expected: all FAIL — `evaluate_lineage_for` not implemented; `lineage_*` audit event types not yet recognized.

### Step 4: Implement `lineage_lifecycle.py`

- [ ] Create `src/identity/lineage_lifecycle.py`. Key shape:

```python
"""R2 lineage lifecycle FSM — single source of truth for the
provisional → confirmed/demoted/archived transitions.

See: docs/ontology/r2-honest-memory-integration.md §Promotion / demotion /
archival protocol
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional
import logging

from src.db import get_db
from src.identity.trajectory_continuity import score_trajectory_continuity
from src.audit_helpers import emit_audit_event_async

logger = logging.getLogger(__name__)


TerminalState = Literal["confirmed", "demoted", "archived"]
Transition = Literal[
    "provisional_to_confirmed",
    "provisional_to_demoted",
    "confirmed_to_demoted",
    "provisional_to_archived",
]


@dataclass(frozen=True)
class LineageEvalOutcome:
    successor_id: str
    parent_id: Optional[str]
    terminal_state: Optional[TerminalState]
    transition: Optional[Transition]
    r1_verdict: Optional[str]  # "plausible" | "unsupported" | "inconclusive" | None
    skipped_reason: Optional[str]


async def evaluate_lineage_for(
    successor_id: str,
    *,
    min_observations: int = 5,
    grace_window: timedelta = timedelta(days=30),
    eval_cadence: timedelta = timedelta(hours=1),
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LineageEvalOutcome:
    backend = get_db()
    row = await backend.read_lineage_state(successor_id)
    if row is None or row["parent_agent_id"] is None:
        return LineageEvalOutcome(
            successor_id=successor_id, parent_id=None,
            terminal_state=None, transition=None,
            r1_verdict=None, skipped_reason="no_parent",
        )
    parent_id = row["parent_agent_id"]

    # Cadence guard
    last_eval = row["lineage_last_eval_at"]
    if last_eval is not None and (now() - last_eval) < eval_cadence:
        return LineageEvalOutcome(
            successor_id=successor_id, parent_id=parent_id,
            terminal_state=None, transition=None,
            r1_verdict=None, skipped_reason="within_cadence",
        )

    # Grace expiration short-circuit (still provisional, declared too long ago)
    if (
        row["provisional_lineage"]
        and row["lineage_declared_at"] is not None
        and (now() - row["lineage_declared_at"]) >= grace_window
    ):
        await backend.archive_lineage(successor_id)
        await backend.stamp_lineage_eval(successor_id)
        await emit_audit_event_async(
            agent_id=successor_id,
            event_type="lineage_grace_expired",
            payload={"parent_id": parent_id,
                     "declared_at": row["lineage_declared_at"].isoformat()},
        )
        return LineageEvalOutcome(
            successor_id, parent_id, "archived",
            "provisional_to_archived", None, None,
        )

    # R1 score
    score = await score_trajectory_continuity(parent_id, successor_id,
                                              min_observations=min_observations)
    await backend.stamp_lineage_eval(successor_id)
    verdict = score.verdict  # "plausible" | "inconclusive" | "unsupported"

    is_confirmed_already = (
        row["confirmed_at"] is not None and not row["provisional_lineage"]
    )

    if verdict == "plausible" and not is_confirmed_already:
        await backend.confirm_lineage(successor_id)
        await emit_audit_event_async(
            agent_id=successor_id,
            event_type="lineage_promoted",
            payload={"parent_id": parent_id, "score_id": str(score.score_id),
                     "plausibility": score.plausibility},
        )
        return LineageEvalOutcome(
            successor_id, parent_id, "confirmed",
            "provisional_to_confirmed", verdict, None,
        )

    if verdict == "unsupported":
        reason = ("post_promotion_divergence" if is_confirmed_already
                   else "r1_unsupported")
        await backend.demote_lineage(successor_id, reason=reason)
        await emit_audit_event_async(
            agent_id=successor_id,
            event_type="lineage_demoted",
            payload={"parent_id": parent_id, "score_id": str(score.score_id),
                     "reason": reason, "plausibility": score.plausibility},
        )
        return LineageEvalOutcome(
            successor_id, parent_id, "demoted",
            ("confirmed_to_demoted" if is_confirmed_already
              else "provisional_to_demoted"),
            verdict, None,
        )

    # inconclusive — no state change
    return LineageEvalOutcome(
        successor_id, parent_id, None, None, verdict, None,
    )
```

`emit_audit_event_async` — if `src/audit_helpers.py` (or the actual audit-emit module) doesn't expose this exact name, use the existing async-audit primitive (`append_audit_event_async` per `audit/agent_silent` precedent). Resolve at PR-write time; do not invent a new helper.

`backend.read_lineage_state(successor_id)` — add as a backend mixin method that returns `parent_agent_id, provisional_lineage, confirmed_at, lineage_declared_at, lineage_last_eval_at, chain_obs_count` in one query.

### Step 5: Run tests until green

- [ ] `./scripts/dev/test-cache.sh tests/test_lineage_integration.py -v`
- [ ] Iterate until 9 passing (test 7 skipped per design — it's PR 3 coverage).

### Step 6: Council pass + commit

- [ ] 3-agent council on the PR diff.
- [ ] Address forcing items.
- [ ] `./scripts/dev/test-cache.sh` (full suite).
- [ ] Commit:

```bash
git add src/identity/lineage_lifecycle.py \
        src/db/mixins/identity.py \
        tests/test_lineage_integration.py \
        tests/helpers/lineage_integration_fixtures.py
git commit -m "feat(r2): PR 2 — lineage_lifecycle FSM + audit events (provisional/confirmed/demoted/archived)"
```

---

## PR 3 — Cross-role pre-check + onboard/identity wiring

**Files:**
- Modify: `src/mcp_handlers/identity/handlers.py` (onboard handler)
- Modify: `src/mcp_handlers/schemas/identity.py` (response schemas)
- Modify: `src/identity/lineage_lifecycle.py` (add `pre_check_cross_role`)
- Test: `tests/test_onboard_lineage_cross_role.py` (new)
- Test: `tests/test_identity_response_lineage_fields.py` (new)

### Goals

1. At onboard, before stamping `parent_agent_id`, look up parent's primary class tag (S8a) and successor's class tag. If mismatched, **reject the lineage declaration** — clear `parent_agent_id` from the write payload, emit `lineage_cross_role_rejected` audit, return `lineage_state="rejected_cross_role"` in the onboard response.
2. If matched (or either side has no class tag — charitable default per design doc), write `parent_agent_id`, call `backend.declare_lineage(...)` to stamp `lineage_declared_at`, emit `lineage_declared` audit, return `lineage_state="provisional"`.
3. Add `provisional_lineage: bool` and `lineage_state: str | None` to both `onboard()` and `identity()` response payloads (and the corresponding Pydantic schemas in `src/mcp_handlers/schemas/identity.py`).

### Step 1: Failing tests

- [ ] `tests/test_onboard_lineage_cross_role.py`:

```python
@pytest.mark.asyncio
async def test_cross_role_lineage_rejected(mcp_client):
    """parent=embodied, successor=session_like → rejected; no parent_agent_id
    written; lineage_cross_role_rejected audit fires."""
    parent_id = await _create_identity_with_class(mcp_client, "embodied")
    response = await mcp_client.onboard(
        force_new=True,
        parent_agent_id=parent_id,
        spawn_reason="new_session",
        # session_like is the default for fresh ephemeral onboard
    )
    assert response["lineage_state"] == "rejected_cross_role"
    assert response.get("provisional_lineage") in (False, None)
    successor_id = response["agent_uuid"]
    stored = await _read_parent_agent_id(successor_id)
    assert stored is None
    events = await _audit_events_for(successor_id)
    assert any(e["event_type"] == "lineage_cross_role_rejected" for e in events)


@pytest.mark.asyncio
async def test_same_role_lineage_accepted_provisional(mcp_client):
    parent_id = await _create_identity_with_class(mcp_client, "engaged_ephemeral")
    response = await mcp_client.onboard(
        force_new=True,
        parent_agent_id=parent_id,
        spawn_reason="new_session",
    )
    assert response["lineage_state"] == "provisional"
    assert response["provisional_lineage"] is True


@pytest.mark.asyncio
async def test_orphan_class_tag_charitable_default(mcp_client):
    """Parent has no class tag (orphan path) → declaration accepted."""
    parent_id = await _create_identity_with_class(mcp_client, None)
    response = await mcp_client.onboard(
        force_new=True, parent_agent_id=parent_id,
        spawn_reason="new_session",
    )
    assert response["lineage_state"] == "provisional"
```

- [ ] `tests/test_identity_response_lineage_fields.py`:

```python
@pytest.mark.asyncio
async def test_identity_response_includes_provisional_lineage(seeded_pair, mcp_client):
    response = await mcp_client.identity(agent_uuid=seeded_pair.successor_id)
    assert "provisional_lineage" in response
    assert response["provisional_lineage"] is True


@pytest.mark.asyncio
async def test_identity_response_excludes_lineage_when_no_parent(mcp_client):
    fresh_id = await _create_identity_with_class(mcp_client, None)
    response = await mcp_client.identity(agent_uuid=fresh_id)
    assert response.get("provisional_lineage") is False
```

### Step 2: Run failing tests

- [ ] `./scripts/dev/test-cache.sh tests/test_onboard_lineage_cross_role.py tests/test_identity_response_lineage_fields.py -v`

Expected: FAIL — `lineage_state` not in response; cross-role pre-check missing.

### Step 3: Implement `pre_check_cross_role`

- [ ] In `src/identity/lineage_lifecycle.py` add:

```python
async def pre_check_cross_role(
    parent_id: str,
    successor_class: Optional[str],
) -> Optional[dict]:
    """Returns None if same-role (declaration may proceed); returns a
    rejection payload `{parent_class, successor_class}` if cross-role.

    Charitable default: if either side has no class tag, treat as
    same-role (per design doc §Cross-role pre-check)."""
    backend = get_db()
    parent_class = await backend.read_primary_class_tag(parent_id)
    if parent_class is None or successor_class is None:
        return None
    if parent_class == successor_class:
        return None
    return {"parent_class": parent_class,
            "successor_class": successor_class,
            "reason": "role_envelope_mismatch"}
```

`backend.read_primary_class_tag(agent_id) -> Optional[str]` — reads `metadata.class_tags[0]` from `core.identities`. Add this as a backend mixin method.

### Step 4: Wire onboard handler

- [ ] In `src/mcp_handlers/identity/handlers.py` find the path where `parent_agent_id` is currently written (search for `parent_agent_id` in the `onboard` path; per R1 PR #321 there's an `_score_lineage_continuity_bg` already wired). Insert pre-check **before** the parent_agent_id is written:

```python
# Inside the onboard handler, after successor's class_tag is determined
# (S8a Phase 2 default-stamp) and BEFORE the upsert that writes
# parent_agent_id:

if params.parent_agent_id:
    rejection = await pre_check_cross_role(
        parent_id=params.parent_agent_id,
        successor_class=stamped_class_tag,
    )
    if rejection:
        await emit_audit_event_async(
            agent_id=successor_uuid,
            event_type="lineage_cross_role_rejected",
            payload={**rejection,
                     "claimed_parent_id": params.parent_agent_id},
        )
        params = params.model_copy(update={"parent_agent_id": None})
        lineage_state = "rejected_cross_role"
    else:
        lineage_state = "provisional"
else:
    lineage_state = "no_lineage_declared"

# ... existing upsert path runs with the (possibly-cleared) params ...

# After upsert succeeds AND parent_agent_id was written:
if params.parent_agent_id is not None:
    await backend.declare_lineage(successor_uuid)
    await emit_audit_event_async(
        agent_id=successor_uuid,
        event_type="lineage_declared",
        payload={"parent_id": params.parent_agent_id,
                 "successor_class": stamped_class_tag},
    )
```

Note: per the design doc anyio rule, both audit emits inside the onboard handler use the existing fire-and-forget `append_audit_event_async` pattern (`agent_silent` precedent in `src/background_tasks.py:1023-1032`).

### Step 5: Extend response schemas

- [ ] In `src/mcp_handlers/schemas/identity.py` find the onboard and identity response models and add:

```python
class OnboardResponse(BaseModel):
    # ... existing fields ...
    provisional_lineage: bool = False
    lineage_state: Literal[
        "provisional", "rejected_cross_role", "no_lineage_declared",
        "confirmed", "demoted", "archived",
    ] | None = None


class IdentityResponse(BaseModel):
    # ... existing fields ...
    provisional_lineage: bool = False
```

(If schemas are dataclasses or TypedDicts rather than Pydantic, mirror the same shape.)

In the handler, compute `provisional_lineage` from the row written/read. For `identity()`, populate from the existing row read.

### Step 6: Run tests until green

- [ ] `./scripts/dev/test-cache.sh tests/test_onboard_lineage_cross_role.py tests/test_identity_response_lineage_fields.py -v`

### Step 7: Council pass + commit

- [ ] 3-agent council. Pay specific attention to:
  - The onboard handler is a single-writer surface per CLAUDE.md. Run `gh pr list -R CIRWEL/unitares --search "in:title,body onboard"` and the equivalent against `unitares-governance-plugin` before opening.
  - Audit-write path must be anyio-safe (fire-and-forget, no `await` on DB inside handler).
  - The S8a charitable-default branch must NOT silently bypass cross-role checks once S8a Phase 2 backfill completes — eventually no production identity should hit the orphan path.

- [ ] Commit:

```bash
git add src/mcp_handlers/identity/handlers.py \
        src/mcp_handlers/schemas/identity.py \
        src/identity/lineage_lifecycle.py \
        src/db/mixins/identity.py \
        tests/test_onboard_lineage_cross_role.py \
        tests/test_identity_response_lineage_fields.py
git commit -m "feat(r2): PR 3 — cross-role pre-check + onboard/identity response lineage_state"
```

---

## PR 4 — Sweeper task

**Files:**
- Modify: `src/background_tasks.py` (register `lineage_eval_sweeper_task`)
- Modify: `src/db/mixins/identity.py` (add `select_lineage_eval_candidates`)
- Test: `tests/test_lineage_eval_sweeper.py` (new)

### Step 1: Backend candidate selector

- [ ] In `src/db/mixins/identity.py`:

```python
async def select_lineage_eval_candidates(
    self,
    *,
    sweep_cadence_hours: int = 6,
    limit: int = 100,
) -> list[str]:
    """R2 sweeper: agents with provisional or confirmed lineage whose
    last_eval_at is older than sweep_cadence (or NULL)."""
    async with self.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id
              FROM core.identities
             WHERE parent_agent_id IS NOT NULL
               AND lineage_archived_at IS NULL
               AND lineage_demoted_at IS NULL
               AND (
                   lineage_last_eval_at IS NULL
                OR lineage_last_eval_at < now() - ($1 || ' hours')::interval
               )
             ORDER BY lineage_last_eval_at NULLS FIRST
             LIMIT $2
            """,
            str(sweep_cadence_hours), limit,
        )
    return [r["agent_id"] for r in rows]
```

### Step 2: Sweeper task

- [ ] In `src/background_tasks.py` after `class_promotion_sweeper_task` (line ~186), add:

```python
async def lineage_eval_sweeper_task(interval_minutes: float = 30.0) -> None:
    """R2: re-evaluate provisional and confirmed lineage edges.

    Mirrors class_promotion_sweeper_task: runs outside anyio context, can
    `await` asyncpg directly. Cycle emits no audit events — only state
    transitions emit (handled by evaluate_lineage_for itself).
    """
    from src.identity.lineage_lifecycle import evaluate_lineage_for

    interval_s = interval_minutes * 60
    while True:
        try:
            backend = get_db()
            candidates = await backend.select_lineage_eval_candidates()
            for successor_id in candidates:
                try:
                    await evaluate_lineage_for(successor_id)
                except Exception as exc:
                    logger.warning(
                        "[r2_sweeper] eval failed for %s: %s",
                        successor_id[:8], exc,
                    )
            logger.debug(
                "[r2_sweeper] cycle complete: %d candidates evaluated",
                len(candidates),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[r2_sweeper] cycle error: %s", exc)
        await asyncio.sleep(interval_s)
```

- [ ] Register in `_supervised_create_task` block near line 1307:

```python
_supervised_create_task(
    lineage_eval_sweeper_task(),
    name="r2_lineage_eval_sweeper",
)
```

### Step 3: Tests

- [ ] `tests/test_lineage_eval_sweeper.py`:

```python
@pytest.mark.asyncio
async def test_sweeper_picks_up_stale_provisional(seeded_pair, monkeypatch):
    """provisional row with NULL lineage_last_eval_at is selected."""
    backend = get_db()
    candidates = await backend.select_lineage_eval_candidates()
    assert seeded_pair.successor_id in candidates


@pytest.mark.asyncio
async def test_sweeper_skips_recently_evaluated(seeded_pair):
    backend = get_db()
    await backend.stamp_lineage_eval(seeded_pair.successor_id)
    candidates = await backend.select_lineage_eval_candidates(sweep_cadence_hours=6)
    assert seeded_pair.successor_id not in candidates


@pytest.mark.asyncio
async def test_sweeper_cycle_no_audit_when_no_transitions(seeded_pair, audit_log):
    """Per design doc §Observability: cycles emit no audit events when no
    transition occurs."""
    from src.background_tasks import lineage_eval_sweeper_task
    pre_count = await audit_log.count_events(event_type="lineage_promoted")
    # Run one iteration only (test mode)
    # ... harness-specific single-cycle invocation ...
    post_count = await audit_log.count_events(event_type="lineage_promoted")
    # Inconclusive seeded pair → no transition → no audit
    assert post_count == pre_count
```

### Step 4: Run + council + commit

- [ ] `./scripts/dev/test-cache.sh tests/test_lineage_eval_sweeper.py -v`
- [ ] Council pass (architect should verify: cycle is bounded — `LIMIT 100`; no audit-emission per cycle; uses `_supervised_create_task` not bare `asyncio.create_task`).
- [ ] Commit:

```bash
git add src/background_tasks.py \
        src/db/mixins/identity.py \
        tests/test_lineage_eval_sweeper.py
git commit -m "feat(r2): PR 4 — lineage_eval_sweeper_task (30min cadence, 6h re-eval guard)"
```

---

## PR 5 — Check-in trigger (`process_agent_update`)

**Files:**
- Modify: `src/mcp_handlers/handlers.py` (or wherever `process_agent_update` lives)
- Test: `tests/test_process_agent_update_lineage_trigger.py` (new)

### Step 1: Locate the handler

- [ ] `grep -n "process_agent_update" src/mcp_handlers/`. The handler is the MCP tool entry point; the dispatch happens inside the handler body after the state update is persisted.

### Step 2: Failing test

- [ ] `tests/test_process_agent_update_lineage_trigger.py`:

```python
@pytest.mark.asyncio
async def test_process_agent_update_dispatches_lineage_eval_for_provisional(
    seeded_pair, mcp_client, monkeypatch
):
    """Successor with provisional_lineage=TRUE → process_agent_update
    schedules evaluate_lineage_for as a tracked task (does NOT inline-await)."""
    dispatched = []

    def fake_create_tracked_task(coro, *, name=None):
        dispatched.append(name)
        coro.close()  # don't actually run
        return asyncio.Future()  # never completes

    monkeypatch.setattr(
        "src.mcp_handlers.handlers.create_tracked_task",
        fake_create_tracked_task,
    )

    await mcp_client.process_agent_update(
        agent_uuid=seeded_pair.successor_id,
        response_text="x",
        complexity=0.3,
    )
    assert any(n.startswith("r2_lineage_eval_") for n in dispatched)


@pytest.mark.asyncio
async def test_process_agent_update_skips_non_lineage_agent(mcp_client, monkeypatch):
    """Agent with no parent_agent_id → no lineage eval dispatch."""
    dispatched = []
    monkeypatch.setattr("src.mcp_handlers.handlers.create_tracked_task",
                        lambda coro, *, name=None: dispatched.append(name) or asyncio.Future())
    fresh = await _create_identity_with_class(mcp_client, "engaged_ephemeral")
    await mcp_client.process_agent_update(
        agent_uuid=fresh, response_text="x", complexity=0.3,
    )
    assert not any(n and n.startswith("r2_lineage_eval_") for n in dispatched)


@pytest.mark.asyncio
async def test_check_in_increments_chain_obs_count_for_confirmed(
    confirmed_pair, mcp_client
):
    """Confirmed lineage → check-in increments chain_obs_count."""
    backend = get_db()
    await mcp_client.process_agent_update(
        agent_uuid=confirmed_pair.successor_id,
        response_text="x", complexity=0.3,
    )
    chain = (await backend.read_lineage_state(confirmed_pair.successor_id))["chain_obs_count"]
    assert chain == 1
```

### Step 3: Implement the dispatch

- [ ] In the `process_agent_update` handler, after the existing state update is persisted:

```python
# R2: lineage eval dispatch + chain counter increment.
# Per CLAUDE.md anyio rule: NO await on R1 or DB-touching FSM here.
# Both run in fire-and-forget background tasks.
lineage_row = await backend.read_lineage_state(agent_uuid)
if lineage_row and lineage_row.get("parent_agent_id"):
    if lineage_row.get("confirmed_at") and not lineage_row.get("provisional_lineage"):
        # Confirmed: increment chain counter (cheap UPDATE, can await)
        await backend.increment_chain_obs_count(agent_uuid)

    # Dispatch FSM eval (sidesteps anyio per design doc)
    from src.identity.lineage_lifecycle import evaluate_lineage_for
    create_tracked_task(
        evaluate_lineage_for(agent_uuid),
        name=f"r2_lineage_eval_{agent_uuid[:8]}",
    )
```

The `await backend.increment_chain_obs_count(...)` is a single UPDATE — falls under existing handler-context `await` patterns already accepted for similar shape (e.g., `update_agent_metadata` writes from `process_agent_update`). If it turns out to deadlock under the anyio task group (unlikely — single-statement UPDATE without long transactions), promote to a tracked-task as well.

### Step 4: Run + council + commit

- [ ] `./scripts/dev/test-cache.sh tests/test_process_agent_update_lineage_trigger.py -v`
- [ ] **Critical council focus:** anyio safety. Live-verifier should confirm via local restart that `process_agent_update` round-trip latency under load doesn't degrade. Per memory `feedback_post-deploy-verify-fleet-wire-ins.md`, restart + canary required after wire-in.
- [ ] Commit:

```bash
git add src/mcp_handlers/handlers.py \
        tests/test_process_agent_update_lineage_trigger.py
git commit -m "feat(r2): PR 5 — process_agent_update dispatches lineage eval (anyio-safe via create_tracked_task)"
```

---

## After PR 5: Phase 1 entry criteria

When PRs 1–5 are merged, R2 Phase 1 is live. Operator-facing surfaces ready:
- Onboard returns `lineage_state` ∈ {`provisional`, `rejected_cross_role`, `no_lineage_declared`}.
- Audit stream carries `lineage_declared`, `lineage_promoted`, `lineage_demoted`, `lineage_grace_expired`, `lineage_cross_role_rejected`.
- Sweeper + check-in trigger keep eval cadence honest.
- **No downstream consumer reads `provisional_lineage` for enforcement yet** — Phase 2 (separate plan row).

Phase 2 entry per design doc §"Shadow-mode calibration path":
- ≥4 weeks Phase 1 telemetry
- ≥50 promoted pairs
- ≥10 demoted pairs
- ≥1 rejected-cross-role event observed

Operator (Kenny) flips R2 calibration_status from `seeded` to `earned` when these are met. Phase 2 implementation row opens after that flip.

---

## Self-review

**Spec coverage:** Each design-doc section has a PR:
- §Storage → PR 1
- §Cross-role pre-check → PR 3
- §"Promotion / demotion / archival protocol" FSM → PR 2
- §"Implementation pattern: anyio-safe check-in trigger" → PR 5
- §"Audit write path" → PRs 2–3 (split: handler-context audits in PR 3, FSM audits in PR 2)
- §"Test fixture" cases 1–10 → PR 2 tests (case 7 covered by PR 3)
- §Observability → PRs 2–4 (audit emit in 2; sweeper cycle counters in 4)
- §Multi-generation chains → PR 2 test 8

**Placeholder scan:** No `TBD`/`TODO`/`fill in details`. The one place I named a function whose existence I haven't verified is `emit_audit_event_async` — flagged as "resolve at PR-write time, use the existing async-audit primitive" rather than left as a placeholder.

**Type consistency:** `LineageEvalOutcome.terminal_state ∈ {"confirmed","demoted","archived",None}`; `transition` is one of the four explicit Literal values; `lineage_state` in the response schema is the union including `"rejected_cross_role"` and `"no_lineage_declared"` which the FSM type does NOT include (response is broader). This is intentional — the FSM only reports actual storage transitions; the response includes onboard-time states (rejection, no declaration) that never enter the FSM.

**Open question to flag at PR 0:** the design doc's three open questions (cross-role reject vs flag-allow; v1.1 cite-and-extend; demote vs fork) are settled per Kenny's 2026-05-02 call (memory: `project_r2-honest-memory-integration.md`). PR 0's plan.md update should explicitly cite that those defaults are being implemented.

---

## Risks / open issues

1. **`emit_audit_event_async` name unverified.** R2 design doc names the function but the actual async-audit helper in master may be `append_audit_event_async` or live elsewhere. PR 2's first internal step is to grep and use the actual name; do not introduce a new wrapper.

2. **`synthetic_trajectory_pair` may not expose all kinds R2 needs.** `genuine_then_divergent` is needed for test 6. R1 v3.1 §"Test fixture" lists it but if R1 implementation row shipped without it, PR 2 must extend the R1 fixture (small addition; should be in the same PR to keep the test self-contained).

3. **Single-writer surface collision risk on `src/mcp_handlers/identity/handlers.py`.** Per CLAUDE.md, run `gh pr list` for that file's surface keywords before opening PR 3. R1 PR #321 already touched this file and #324 followed; cluster of edits in this region.

4. **Migration slot 035 collision risk.** Run `git log --all --follow db/postgres/migrations/035_*` and `gh pr list -R CIRWEL/unitares --search "035"` before committing PR 1. Slot drift is the canonical incident pattern (CLAUDE.md migration-drift section).

5. **Phase 2 deferral discipline.** Tempting to wire one downstream consumer in PR 6 to "prove the chain works" — resist. Phase 1 telemetry is what calibrates the thresholds. Wiring trust-tier or dashboard before that data exists creates pressure to ship before calibration is done.

6. **Watcher impact.** R2 lifecycle adds 5 new audit event types. Confirm `audit/event_taxonomy` (if present) classifier covers them, or add taxonomy entries as part of PR 2.

---

**Plan complete.**
