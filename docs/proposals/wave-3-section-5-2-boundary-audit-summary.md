# Wave 3 §5.2 boundary-cost audit — committed summary (2026-06-10)

This is the CI-checkable summary required by `scripts/dev/check-wave3-ode-prereq.sh`
before any `elixir/handler_dispatch/` commit (§14 row 1; the full analysis lives in
the operator-local `docs/handoffs/wave-3-section-5-2-boundary-audit-2026-06-10.md`,
gitignored per RFC convention). Reclassifications are folded into §5.2/§5.3 as
RFC v0.3.3 in the same PR that adds this file.

## Method

Per §14: every helper in §5.2's "stays Python" table was profiled —
micro-benchmarked compute (best-of-5 × ≥500 calls, representative inputs) against
measured boundary-crossing cost (PR #599 baseline: 3.2–3.5 ms floor for minimal
crossings; 40–255 ms for contended acquire) on the live request mix
(1 dialectic session in 30 days; finalize ≈ 1/session).

## Headline result

Compute spans 0.04–23 µs/call — **every** helper is crossing-dominated by raw
ratio (139×–80,000×), so the ratio alone is not the rule. The discriminator is
call placement under §5.6's single compute endpoint:

- **Bundled** in the `synthesize`/`select_reviewer` payload → marginal crossing
  cost zero → stays Python. Confirmed for: `calculate_authority_score`,
  `_normalize_condition_terms`, `_semantic_similarity_terms`,
  `_merge_proposals`, `_conditions_conflict`.
- **Standalone from §5.1 coordination that ports** → the
  `_compare_against_timeout` pattern → reclassified PORTS-to-BEAM:

| Reclassified | Caller (all §5.1) | Gate |
|---|---|---|
| `_read_proposed_conditions` | handlers 281/1061/1350 | unit parity only |
| `check_hard_limits` | handlers 1434 (reassign) | regex-dialect golden tests (Python `re` vs PCRE) |
| `_parse_timestamp` (§5.3 flip) | auto_resolve | none — `DateTime.from_iso8601/1` |
| `Resolution.hash` | execute_resolution 153/195 | canonical-payload parity family |
| `Resolution.sign` + `compute_signature` + `canonical_payload` | finalize 791/893–894 | **golden-vector byte-parity** (see below) |

- `verify_signatures` goes **DUAL**: BEAM verifies at runtime; Python retains
  verification for archival reads of stored v1/v2 resolutions.
- `condition_parser` row **splits**: `parse_condition` (pure) ports-or-bundles;
  `apply_condition` is async state-mutation taking `mcp_server`
  (resolution.py:59) — mis-filed as computation; it belongs to §5.1's
  `execute_resolution` port.
- Calibration row was **mis-scoped**: `backfill_calibration_from_dialectic` is
  its own MCP tool (not a helper-crossing question);
  `update_calibration_from_dialectic`(+`_disagreement`) are
  imported-never-called — dead-wiring to resolve before the implementation gate.

## The one load-bearing risk

Porting the signing cluster is gated on **canonical-payload byte-parity**:
signatures are HMACs over `json.dumps`-canonicalized bytes, and the Elixir port
must reproduce them byte-identically (key order, escaping, separators) or every
stored v2 signature fails verification. Required gate: golden-vector parity
tests (N stored payload→signature pairs both runtimes must reproduce — the §8
golden discipline extended to signatures). Documented fallback if parity proves
brittle: keep signing Python-side as one bundled `sign_resolution` compute mode
(1 crossing per session lifetime ≈ 4 ms).

## Absolute-cost honesty

At the live mix, every reclassification saves milliseconds **per month**. These
are architectural verdicts (don't hard-code the standalone-crossing pattern
into the port), not performance work. The latency case for Wave 3 lives in
disconfirmer A.1's coordination-bound p99 — not here.

## Side observations (out of audit scope, flagged to operator)

- `check_hard_limits`'s only production call site is the reviewer-reassign
  path; the module docstring implies a resolution-accept safety gate that does
  not appear to exist.
- The per-outcome calibration update the §5.2 row describes does not currently
  run anywhere.
