# Memory Authority, Promotion Receipts, and Topic Synthesis

Reference companion to `skills/knowledge-graph/SKILL.md`. Load when promoting
imported memory (`knowledge(action="promote")`), verifying
`provenance.transition_receipt`, running `knowledge(action="synthesize")`, or
diagnosing anonymous vs. strict-identity KG write handling.

## Memory Authority Tiers

Default search is authority-aware (retrieval preference, not a truth verdict):

- **`imported_context`**: rows matching the harness-neutral
  `source-<provider>-memory` tag pattern (e.g. `source-claude-memory`; topic
  words like `memory-sync` or `imported-memory` do not trigger it). Down-ranked
  in close relevance contests against native findings and governed claims,
  omitted from queryless listings (`imported_omitted` + hint) and check-in
  memory suggestions. Pass `authority_mode="all"` or filter solely by
  `source-<provider>-memory` tags to inspect that lane directly. Newest-first
  reads (`sort_by="created_at"` or queryless date windows) apply no authority
  reorder.
- **`channel_message`**: rows carrying a `channel-<topic>` + `to-<agent>` tag
  pair or a `[channel:<topic>]` summary prefix. Receive a stronger close-contest
  down-rank; a `channel-*` tag filter lifts the down-rank only for channel
  messages on that lane.
- **`native_finding`**: ordinary stores.
- **`governed_claim`**: created only via `knowledge(action="promote")`, which
  attaches a server-authored receipt that ordinary `store` arguments cannot
  forge.

## Promoting Imported Memory (`knowledge(action="promote")`)

```
knowledge(
  action: "promote",
  discovery_id: "<imported-memory-id>",
  evidence_ids: ["<native-finding-id>"],
  summary: "Bounded claim accepted after verification",
  verification_basis: "Repository configuration and integration test agree",
  decision_standard: "At least one non-memory artifact independently supports it"
)
```

- Requires a caller-owned registered identity, refuses self-corroboration by
  the source row, and refuses imported memories as independent evidence.
- Creates a new linked row without rewriting or deleting the source. "Governed"
  means the transition is attributable and evidence-linked, not infallible.
- **Transition receipt (`provenance.transition_receipt`, v1)**: unsigned,
  content-addressed statement inside the UNITARES server's authority domain
  covering the exact accepted creation projection and naming which fields the
  server did and did not verify. It does not prove factual correctness,
  independent review, operator approval, or external non-repudiation, and its
  empty `authorizes` list grants no authority.
- **Verifying the receipt**: verify against the complete durable row (assemble
  all `details` pages first — summary or paginated fragments fail explicitly as
  an incomplete subject projection). A later edit intentionally causes
  `subject_hash_mismatch` rather than rewriting the historical receipt.

## Strict Identity vs. Anonymous Fallback on KG Writes

- A write whose binding was only inferred (`proof_origin: "server_inferred"`) is
  never credited to the matched agent: low/medium `store`, `note`, or `update`
  writes under an anonymous writer ID (`anonkg_*`), while under strict identity
  high/critical writes are refused (`identity_required`) and nothing is written.
  Substrate-earned residents keep their fingerprint attribution by design.
- Passing `agent_id` with no proved binding (`client_session_id`, continuity
  token, or equivalent) is refused with `identity_required` regardless of
  severity.
- `supersede`, `cleanup`, `synthesize`, `promote`, and `audit` have no
  anonymous fallback and refuse unbound calls (accepting server-inferred
  bindings only for substrate residents).

## Topic Synthesis (`knowledge(action="synthesize")`)

Compounds discoveries under a tag into a rolled-up summary row
(`type="topic_rollup"`, deterministic ID `rollup::<topic>`, tagged `rollup`),
upserting in place so it is searchable via `knowledge(action="search", tags=["rollup"])`
before query time (instead of only on-read `search(..., synthesize=true)`).

- Controls: `topic="..."` (single tag; omitted sweeps densest topics),
  `dry_run=true` (preview without writing; note `synthesize` persists by
  default unless `dry_run=true`), `min_members` (default 3), `use_llm=false`
  (force deterministic narrative).
- **Traceability validation**: generated rollup statements cite supporting
  discovery IDs. The server verifies every cited ID belongs to the bounded
  source sample (capped at 12 discoveries, 1,000-char excerpts each) and falls
  back to a deterministic source list on missing/invalid references. Reference
  validation establishes traceability, not factual entailment or complete topic
  coverage — inspect source IDs before relying on a claim.
- **Cadence and `consolidation_hint`**: run on demand or on a periodic cadence,
  never on every write. Server schedule is controlled by
  `UNITARES_KG_SYNTHESIS_SCHEDULE`: `shadow` (default — deterministic preview
  logged only), `apply` (writes as `system:kg-synthesis` with local LLM +
  deterministic fallback), or `off`. When a `store` finds 3+ open
  near-duplicates, it returns a `consolidation_hint` and queues shared tags
  (in memory; lost on restart) for the next synthesis pass — still supersede or
  resolve the duplicates, because a rollup leaves each open.
