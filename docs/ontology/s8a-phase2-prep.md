# S8a Phase 2 — Prep (2026-04-29)

**Companion to:** `docs/ontology/s8a-tag-discipline-audit.md` (Phase 1)
**Status:** Prep diagnostic. Not a ratification artifact. Tomorrow's session (≥1-week-of-data trigger fires 2026-04-30) opens the actual Phase-2 PR using this as the data input.

## Why this exists

Operator decision (b) from the Phase-1 audit gated threshold-drafting on ≥1 week of Phase-1 data accumulating. Phase-1 default-stamp landed 2026-04-23 (PR #121). Today is day 6. This doc captures the day-6 distribution, surfaces what the data revealed about Phase-1 itself, and reframes Phase-2 scope.

## What changed: Phase 2 isn't only a sweep, it's also a stamp-gap fix

The Phase-1 audit framed Phase 2 as a `ephemeral → session_like` promotion sweep. Day-6 data shows that framing is incomplete: **37% of Phase-1-window identities are untagged**, meaning a sweep against the current base would calibrate session-like thresholds against a biased sample (the engaged Claude-Code-via-MCP-stdio path is over-represented; claude_desktop, Codex, and named residents are missing).

If decision (d) backfills 3180 archived records under the same rule, those biases propagate retroactively.

## Day-6 corpus shape

```sql
-- 2026-04-23 → 2026-04-29 22:39 (current)
SELECT class_tag, COUNT(*) FROM (
  SELECT CASE
    WHEN metadata->'tags' @> '["ephemeral"]'::jsonb THEN 'ephemeral'
    WHEN metadata->'tags' @> '["session_like"]'::jsonb THEN 'session_like'
    WHEN metadata->'tags' @> '["resident"]'::jsonb THEN 'resident'
    WHEN metadata->'tags' @> '["substrate_earned"]'::jsonb THEN 'substrate_earned'
    WHEN metadata->'tags' IS NOT NULL AND jsonb_array_length(metadata->'tags') > 0 THEN 'other_tags'
    ELSE 'untagged'
  END AS class_tag
  FROM core.identities WHERE created_at >= '2026-04-23'
) t GROUP BY 1;
```

| Class tag | n | % |
|---|---|---|
| ephemeral | 113 | 62% |
| untagged | 67 | 37% |
| other_tags | 2 | 1% |
| **Total** | **182** | |

### Phase-1 stamping gaps (the 67 untagged + 2 mis-tagged)

| Cohort | n | Gap |
|---|---|---|
| Anonymous (no label, no tags) | 41 | Probably external probes / unbound MCP clients — leaving untagged is defensible |
| `claude_desktop-claude` | 10 | Phase-1 default-stamp doesn't fire on Claude Desktop's onboard path |
| `claude_code-*` | 10 | Subset Phase-1 missed (the bigger group IS stamped — narrow path-specific gap) |
| Named residents (Sentinel, Iris, Mnemos, "Codex S21-b follow-up") | 4 | Should be `resident`, not untagged |
| `["Iris"]` literal-string-as-tag | 1 | Class-tag-discipline bug — the label was written into the tags array |
| Chronicler `["persistent", "autonomous", "cadence.24hr"]` | 1 | Role/cadence tags, not class taxonomy |

## Update-activity distribution within the 113 ephemeral cohort

```sql
SELECT
  CASE WHEN (metadata->>'total_updates')::int = 0 THEN '0'
       WHEN (metadata->>'total_updates')::int BETWEEN 1 AND 2 THEN '1-2'
       WHEN (metadata->>'total_updates')::int BETWEEN 3 AND 5 THEN '3-5'
       WHEN (metadata->>'total_updates')::int BETWEEN 6 AND 10 THEN '6-10'
       WHEN (metadata->>'total_updates')::int BETWEEN 11 AND 25 THEN '11-25'
       WHEN (metadata->>'total_updates')::int BETWEEN 26 AND 100 THEN '26-100'
       ELSE '>100' END AS bucket,
  COUNT(*) FROM core.identities
WHERE created_at >= '2026-04-23'
  AND metadata->'tags' @> '["ephemeral"]'::jsonb
GROUP BY 1;
```

| Updates | n | % of 113 | Cumulative-from-bottom |
|---|---|---|---|
| 0 | 62 | 55% | — |
| 1–2 | 13 | 12% | 13 (12%) ≥1 |
| 3–5 | 13 | 12% | 38 (34%) ≥3 |
| 6–10 | 9 | 8% | 25 (22%) ≥6 |
| 11–25 | 13 | 12% | 16 (14%) ≥11 |
| 26–100 | 1 | 1% | 3 (3%) ≥26 |
| >100 | 2 | 2% | 2 (2%) >100 |

Top end (sanity check, all genuine session-like Claude Code work — no misclassified residents):
- `unitares` (Claude_Code_Opus_20260425): 446 updates, 4d age
- `claude_code-opus` (Claude_Opus_4_7_20260424): 197 updates, 5d age

## Recommended threshold: `total_updates ≥ 3`

Reasoning:
- **No natural gap exists within the engaged cohort.** The 1-2 / 3-5 / 6-10 / 11-25 spread is roughly flat (12 / 12 / 9 / 12 percent). So the only honest break is "0 vs engaged."
- **`≥1` is too aggressive.** One stray check-in promotes single-shot probes (e.g. `cursor_binding_fix_probe`, `cursor_binding_fix_probe_2`).
- **`≥3` means "agent did onboard + ≥2 substantive updates."** Captures repeated work without trapping accidental probes.
- **Resulting promotion: 38 of 113 (34%).**

This is a recommendation for the day-7 ratification, not a binding number. Day-7 data may shift it.

## Recommended Phase-2 PR shape (single coherent PR)

Three items, not two:

1. **(a) Class addition** — `session_like` keyed entry in `governance_config.py` scale maps. Alias-to-default per Steward / Chronicler precedent (corpus doesn't exist until promotion runs).

   ```python
   # DELTA_NORM_MAX_BY_CLASS (~line 847)
   "session_like": ScaleConstant(
       name="DELTA_NORM_MAX[session_like]", value=0.2018, measured_on="2026-04-30",
       corpus_size=0, percentile=None, provenance="alias",
       notes="Alias to default. session_like class added by S8a Phase 2; "
             "ephemeral → session_like promotion produces the corpus. "
             "Re-run scripts/calibrate_class_conditional.py once corpus exists."),

   # HEALTHY_OPERATING_POINT_BY_CLASS (~line 865)
   "session_like": (0.7264, 0.7934, 0.2364),  # alias=default
   ```

   `S_SCALE_BY_CLASS` / `I_SCALE_BY_CLASS` / `E_SCALE_BY_CLASS` are currently `{}`. No entry needed there until `scripts/calibrate_class_conditional.py` runs.

2. **(c) Promotion rule** — `ephemeral → session_like` when `total_updates ≥ 3`. Ships as a Steward-embedded sweep or a launchd task; one-shot pass plus ongoing reclassification.

3. **(a-bis) Phase-1 stamp gap fixes** — *new scope vs original audit:*
   - claude_desktop onboard path: trace where the default-stamp doesn't fire
   - Codex onboard path: same trace (the `Codex S21-b follow-up` identity at 22:39:32 today is a witness)
   - Named-resident detection: Sentinel, Iris, Mnemos, etc. should land in `resident` not untagged
   - Discipline bugs: `["Iris"]` literal-as-tag, Chronicler's role/cadence tags

   Without these, decision (d)'s archival backfill carries the same biases. Should ship in the same PR or as an immediate predecessor — not deferred.

## Decision (d) — backfill scope

3180 archived records. Same rule as (a-bis) + (c). One-shot pass after the live rules ship. Cheap once rules are agreed.

## Open questions for tomorrow's session

1. **Threshold sensitivity.** Day-7 data may add or shift agents in the 1-2 bucket. If 5+ new agents arrive in 1-2 between now and ratification, revisit the cut.
2. **Stamping-gap fix sequencing.** Bundle into the Phase-2 PR or ship a-bis first as a tiny standalone? My read: bundle. Council review covers both.
3. **Council requirement.** Calibration-consequence territory — S6 thresholds depend on Phase 2's class composition. Per memory entry "Council also for load-bearing implementation," parallel independent review pre-merge.

## What this prep is NOT

- Not a threshold ratification (decision (b) gates on day 7)
- Not the Phase-2 PR (separate session opens that tomorrow with this as input)
- Not a full Phase-1 stamping-path audit — only what surfaced in the cohort sample. The actual onboard-path traces (claude_desktop / Codex) are tomorrow's investigation work.

## Handoff to day-7 session

```
Pick up: docs/ontology/s8a-phase2-prep.md (this file)
Re-run the three queries above; check for shape drift in 24h.
Open Phase-2 PR with three items: (a) class entry, (c) promotion rule, (a-bis) stamp gap fixes.
Council review pre-merge.
After merge: decision (d) backfill (one-shot pass).
```
