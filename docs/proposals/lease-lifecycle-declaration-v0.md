---
status: PROPOSED — not implemented, no code changed
authored: 2026-08-11
amends: surface-lease-plane-v0.md (§7.10 force-release, §7.13 substrate state, §4 architecture)
trigger: 7h permanent strand of `resident:/steward_eisv_sync` (2026-08-10 16:28 → 23:45), cleared manually
review_target: |
  NOT council-reviewed. Written after three refuted fix attempts on this subsystem
  in one hour (§6) — the refutations are the most load-bearing content here and
  should be read before any alternative is proposed.
---

# Lease lifecycle must be declared, not inferred from the surface scheme

## 1. Problem

`acquire_for_surface/1` (`http_router.ex`) chooses a lease's death semantics from
its **surface scheme prefix**:

```elixir
if file:// or maintenance:/ or agent:/ -> acquire_remote_heartbeat  # pure TTL row, self-heals
else                                    -> acquire_local_beam       # auto-renewing holder
```

The comment above it is explicit that this is deliberate and narrowly scoped:
residents "are intentionally long-lived and rely on server-side auto-renew for
continuity, so the routing is scoped to the file + agent + maintenance schemes
precisely so it CANNOT regress resident coordination."

That reasoning is correct for what it describes. The defect is that `resident:/`
does not contain one kind of lease. It contains two, with **opposite** required
behaviour on caller death:

| | presence | mutual exclusion |
|---|---|---|
| example | `resident:/steward` | `resident:/sentinel_cycle`, `vigil_cycle`, `steward_eisv_sync`, `chronicler_scrape`, `watcher_scan_commits_*` |
| acquired | once per process, held for its life | per work cycle, released at the end |
| acquires / 24h (measured 2026-08-10) | ~1 | **~3,800** (`sentinel_cycle` alone: 2,999) |
| holder uuid | the resident's stable uuid | `new_holder_uuid()` — fresh random per attempt |
| on caller death | keep renewing (continuity) | **expire** (release the exclusion) |

Both are routed to `acquire_local_beam`, so an exclusion lease gets an
auto-renewing holder. It therefore cannot expire, and cannot be re-attached,
because its holder uuid is random by design.

## 2. Why the random uuid is correct and must not be "fixed"

`lease_advisory_scope` provides **mutual exclusion**: a second concurrent cycle
must observe `held_by_other` and decline. `Repo.acquire` returns the existing row
as `:idempotent` when the requester already holds the surface (`repo.ex:39`).

So giving an exclusion lease a stable holder uuid would make the second
concurrent cycle **idempotently re-attach and proceed**, silently destroying the
mutual exclusion the lease exists to provide. The random uuid is load-bearing.
This is the difference between `resident:/steward` (stable uuid, unstrandable by
construction) and every exclusion surface (random uuid, strandable) — and it is
why the strand cannot be fixed on the client side by changing identity.

## 3. Why a strand is permanent

Four independent recovery mechanisms exist. A single timed-out-but-succeeded
acquire disqualifies all four at once:

| mechanism | why it cannot fire |
|---|---|
| idempotent re-attach (`repo.ex`) | holder uuid is random; the next attempt is a different uuid |
| own-orphan reclaim (#1459 / #1467) | `ReclaimMemory` is an in-process dict (`reclaim.py`); a restart erases the ledger, so a strand predating the current process is unreclaimable forever |
| TTL expiry | the auto-renewing holder renews indefinitely |
| the alarm | fires every cycle, and no running code can act on it |

Observed 2026-08-10: `resident:/steward_eisv_sync` stranded at 16:28:04 under
holder `163ba886…`, renewed at **exactly 100.0s ± 0 across 71 samples** (= TTL/3,
the pure server grid, zero client renews), producing 106 `conflict_held_by_other`
events and a `sentinel_alarm_finding` every ~5 minutes for 7 hours. The current
gov-mcp process started at 21:18 — five hours after the strand — so reclaim was
structurally impossible. Cleared only by manual `force-release`.

## 4. The signal does not exist today

The obvious fix — have the router honour the client's requested `holder_kind`
instead of the scheme — **does not work**, and this is the measurement that
kills it:

```
 surface                             requested_kind  heartbeat_required  acquires/24h
 resident:/sentinel_cycle            local_beam      false               2999
 resident:/vigil_cycle               local_beam      false                262
 resident:/steward_eisv_sync         local_beam      false                220
 resident:/chronicler_scrape         local_beam      false                157
 resident:/watcher_scan_commits_*    local_beam      false                 78 …
```

Every `resident:/` acquire — presence and exclusion alike — requests
`local_beam` with `heartbeat_required=false`. There is nothing in the request to
route on. The scheme prefix is not merely a lossy proxy for lifecycle; it is the
**only** signal the server has.

## 5. Proposal

Add an explicit lifecycle declaration to the acquire contract.

```
POST /v1/lease/acquire
{
  "surface_id": "resident:/vigil_cycle",
  "lifecycle": "exclusive_job",     // NEW: "presence" | "exclusive_job"
  ...
}
```

- `presence` → `acquire_local_beam` (auto-renew). Current behaviour for
  `resident:/`, unchanged.
- `exclusive_job` → `acquire_remote_heartbeat` (pure TTL row, Reaper collects at
  `expires_at`). The path already proven for `file://`, `agent:/`,
  `maintenance:/`, whose own comment states the requirement exclusion leases
  share verbatim: "MUST self-heal if the caller dies without releasing."

Routing precedence becomes: explicit `lifecycle` when present, else the existing
scheme table (so every current caller is unaffected until it opts in).

Properties this yields:

- A strand self-heals in one TTL instead of never. `steward_eisv_sync` (300s)
  would have cleared at 16:33 rather than 23:45.
- Mutual exclusion is preserved — the random holder uuid stays correct and
  unchanged.
- No scheme migration. RFC §7.2.1 deliberately moved `vigil:cycle` **into**
  `resident:/vigil_cycle`; renaming surfaces would fight that decision and churn
  migration 026's generated `surface_kind` column, the taxonomy/class routing,
  and Phase B enforcement scopes.
- No new gate, no liveness heuristic, no shadow-mode arming: the behaviour is
  declared by the caller that knows, not inferred by the server that cannot.

## 6. Refuted alternatives — do not re-derive

Three approaches were proposed and refuted in sequence on 2026-08-10. Each
looked correct until one more layer was checked; they are recorded because the
refutation is the expensive part.

1. **"Shared-cursor starvation in `forced_release_alarm._poll_inner`."** Three
   queries filter on one `last_event_ts` and advance one `max_ts`, so a
   high-frequency stream can drag the cursor past a low-frequency one. Real
   latent fragility — **but not the cause here.** Every `forced` row in the
   window is a `td:/test/force-release-contract-*` fixture, correctly suppressed
   by `_is_reserved_test_surface`. The alarm was silent because there was
   genuinely nothing to report.

2. **"Give work leases the resident's stable uuid."** Would restore idempotent
   re-attach — and **destroy mutual exclusion**, because the second concurrent
   cycle would re-attach and run. See §2.

3. **"Honour the client's requested `holder_kind`."** No distinguishing signal
   exists; every caller requests `local_beam`. See §4.

## 7. Open questions for council

1. Should `lifecycle` be required for new surfaces and defaulted only for
   existing ones, or permanently optional with scheme fallback?
2. Does `exclusive_job` need a distinct `surface_kind` for Phase B enforcement
   scoping, or does it inherit `resident`?
3. `heartbeat_required` already exists and is `false` fleet-wide. Is it the
   right field to overload instead of adding `lifecycle`, and if so what
   distinguishes "heartbeat" from "no auto-renew"?
4. Should the plane refuse an `exclusive_job` acquire carrying a holder uuid it
   has seen before, as a structural guard against the §2 mistake?

## 8. Not in scope

No alerting change. The alarms fired by this class are **true** and should keep
firing; they were also **consequence-free** (steward's EISV output ran
11–13 rows/hour throughout the 7h strand). Suppressing them would hide the
condition rather than remove it. Once §5 ships, the condition stops occurring and
the alarms stop for the right reason.
