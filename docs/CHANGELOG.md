# Changelog

All notable changes to the UNITARES Governance Framework will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

_No unreleased changes yet. New entries accumulate here until the next release bump._

---

## [2.14.0] - 2026-06-28

_Backfill of notable changes merged between 2.13.0 (2026-05-04) and this release. `pyproject.toml`/`VERSION` bumped to 2.14.0._

### Added

- **orchestrator:** BEAM-native ephemeral-agent orchestrator (Layer A, v0) with lineage + `server_url` provisioning for spawned agents (#581, #648, #650, #589, #588)
- **lease-plane:** BEAM hot-code-reload via named node (#570); `agent:/` ephemeral-agent presence scheme, self-healing (#588)
- **wave-3:** §14 prerequisites — shadow DDL, divergence comparator, event types, measurement helpers/lint/lease baseline (starts the 14-day clock) (#597, #599)
- **floor:** identity-free substrate observation sink for un-onboarded sessions (#669)
- **governance:** corroboration grading for outcome events (#681); escalate paused residents to the operator (#687)
- **identity:** lifecycle visibility surface (#683)
- **governance:** cadence-silence — soft detection of active-then-silent agents (#594)
- **kg:** fold tag `normalize()` into the write path + lifecycle synonym pass + backfill (#671)
- **dialectic:** heterogeneous structured-JSON reviewer replaces text-scrape (#563)
- **lifecycle:** bucket test agents separately in `identity_health` counts (#645)
- **sdk:** thread `connect_timeout`/`connect_retries` through `GovernanceAgent` (#560); PyPI-ready packaging + tag-driven trusted-publishing workflow (#649)
- **dashboard:** persist one-time `?operator_token=` handoff into localStorage (#643)

### Changed

- **residents:** roster + progress registry are now deployment config, empty by default (#694)
- **workflow:** unify Codex/Claude GitHub delivery conventions (#680); route stuck work to draft PRs (#651)
- **mirror:** reflect, don't advise — strip prescription from mirror mode (#583); novelty-gate complexity line, disclose proxy basis, surface phi (#603)
- **dashboard:** unify on a neutral color system (color = status) (#561); reduce knowledge polling (#684)
- **watcher:** demote P008 to experimental (#659)
- remove the never-consulted `rate_limit_exempt` decorator flag (#660)

### Fixed

- **governance/eisv:** gate self-relative risk by absolute basin health (#689, #696); floor z-score sensitivity for stable agents + anchor quick-resume to target coherence (#686); scale EISV z-floor by EMA alpha (#699); warmup structural grace suppresses cold-ODE false-pauses post-restart (#577); restore behavioral baseline on DB hydrate (#575)
- **§129:** un-blind the substrate-tax gate (nesting) + stop counting shutdown noise (#576)
- **identity:** strict gate keys on caller-proven binding, not binding-presence (#674, #675); single-flight + PG adopt-winner for operator-token first-use mint race (#646); stop laundering server-injected fingerprint CSID into the strong assurance tier (#682); anonymous auto-mint must not use the reserved `mcp_` prefix (#598); subagent onboards must not displace the driver's fingerprint pin (#604); lineage successor suppresses parent stuck-flag (#677)
- **kg:** close write-path bugs — id collisions, naive ts, batch parity (#673); decouple auto-hybrid term cap from OR-recall cap (#672)
- **kg:** stop cold-storage rows leaking into default search un-down-ranked — search now excludes `cold` unless `include_cold=true` (mirrors archived), and `cold` ranks below `archived` in the blend's status multipliers (#950)
- **calibration:** correct severe lower-bin underconfidence, not just cap it (#668)
- **responses:** honest verdict provenance + reconcile position/thread_size (#670); reconcile six API-response honesty inconsistencies (#665)
- **dashboard:** inner deadline degrades slow fleet read instead of a 15s hang (#666)
- **dialectic:** real timer for stuck-session auto-resolve (#564); exclude non-reasoning agents from reviewer auto-selection (#664)
- **watcher:** P002 required-token + bound-cue drops kill the cap-adjacent false-positive class (#602); checkout-independent state dir + proof-rebind on check-in (#595)
- **anomalies:** label frozen-window anomalies stale instead of presenting them as current (#653)
- **metrics:** zero-observation responses stop asserting seed-derived assessments (#605)
- **sdk:** bound + retry the MCP connect handshake (#559); loud anchor-write failures + typed 503 backoff (#647)

### Documentation

- land the UNITARES trust contract (v0.2, grounded)
- rewrite README for clarity, reduce invented jargon (#695)
- wave-3 §5.2 boundary-cost audit summary + RFC v0.3.3 fold (#600)

### Tests

- strengthen suite and delivery gates (#693); guard `CHRONICLER_SERIES_NAMES` against scraper drift (#697); isolate the watcher suite from the live server and real log (#658)

---

## [2.13.0] - 2026-05-04
### Added

- **sdk:** substrate emission in checkin (RFC §7.13 PRs 4-7 bundled) (#330) (69c5806)
- **governance:** PR 3 — class-aware void threshold (RFC §7.13.6 interim safety net) (#328) (c8a5f73)
- **r1:** §4.3 public KG emission — closes PR 2's deferred AGE write (R1 v3.3-A + v3.2-D) (#324) (b42134b)
- **r1:** §4.1 onboard wiring (marks policy) + R1 implementation handoff doc (#321) (452354b)
- **lease-plane:** PR 1 — migration 034 + Pydantic + Elixir Repo/router substrate_state surface (RFC §7.13) (#322) (37bdf99)
- **r1:** PR 4a trust-tier provisional gate (R1 v3.3-D consumer) (#320) (fecaadb)
- **lease-plane:** migrate force-release CLI to HTTP + §9 integration test (PR 2 of 2) (#313) (e407665)
- **r1:** PR 3 score-side completion — calibration_state + class_tag + verdict degradation + provisional helpers (R1 v3.3 §C,D,G) (#314) (d4740c8)
- **doctor:** elixir_scheme_grammar_lint — fail on Elixir scheme not in grammar CHECK (#311) (8495e9d)
- **sentinel:** batched alarm rule for conflict_held_by_other events (#310) (3ff210d)
- **r1:** PR 2 core primitive — score_trajectory_continuity (R1 v3.3 §A,C,H) (#309) (83e70aa)
- **r1:** PR 1 foundation — migration 031 + reconstruct_eisv_series + epoch backport (R1 v3.3 §A,D,E,F,I) (#306) (f30d192)
- **lease-plane:** RFC §7.10 contract-layer force-release authority (#299) (5b8687f)
- **lease-plane:** align role-holder rejection with RFC §4.4 + §7.3.5 — 200 permission_denied (#297) (17258a4)
- **audit:** §9 reconciliation — alias annotation support + 4 gap-fills (21/1/6) (#295) (cde59af)
- **lease-plane:** audit_rfc_section_9_gates.py — mechanical §9 reconciliation baseline (#291) (006b394)
- **lease-plane:** add R7 in-memory OTP spike (c0f892f)
- **r6:** add episode fork kind to process update context (#287) (bd9fc86)
- **doctor:** add elixir_deprecated_scheme_lint check (Phase B prep, RFC §7.11.8) (#286) (54a789f)
- **lease-plane:** R1 — deprecate-and-finalize super-command (RFC §7.11.2 atomicity) (#285) (91f4290)
- **lease-plane:** Phase A PR 7.5 — Elixir file:// canonicalization (#281) (3d843ff)
- **lease-plane:** Phase A PR 7 — Elixir router server-side canonicalization + transition_handoff INSERT fix (#280) (c34e1f7)
- **lease-plane:** Phase A PR 4 — acquire_with_retry + _urllib_transport HTTP-error coverage (#271) (246ee7a)
- **lease-plane:** Phase A PR 3b — Sentinel forced-release alarm + race-window integration test (#270) (2d59ce9)
- **lease-plane:** Phase A PR 3a — deprecation CLI + migration 028 trigger fix (#269) (a4af34c)
- **lease-plane:** Phase A PR 2.5 — production surface_id migration + AcquireRequest field_validator (#267) (a778539)
- **lease-plane:** Phase A PR 2 — canonicalize.py + AcquireRequest surface_kind drop (#266) (5ad11d1)
- **lease-plane:** Phase A PR 1 — migrations 026/027 + v0.7 model/router drift fixes (#264) (4f50bb5)
- add BEAM lease-plane workers (#263) (9bce80c)
- **chronicler:** Phase A advisory-mode lease-plane wiring on run_cycle (#261) (c31d80c)
- **sentinel:** Phase A advisory-mode lease-plane wiring on run_cycle (#260) (79fbeba)
- **vigil:** Phase A advisory-mode lease-plane wiring on run_cycle (#259) (462612c)
- **ship-sh:** Phase A advisory-mode lease around ship operation (#258) (55ef2af)
- **watcher:** Phase A advisory-mode lease-plane wiring on scan_commits (#256) (108862b)
- **lease-plane:** HTTP endpoint (Plug + Bandit on 127.0.0.1:8788) (#253) (e49703b)
- **lease-plane:** Elixir/OTP coordination kernel scaffold (no HTTP yet) (#251) (7d7d131)
- **s8a:** Phase-2 engaged_ephemeral promotion + Phase-1 stamp-gap fix (#252) (741b088)
- **lease-plane:** capture parallel-session implementation skeleton (7ad5644)
- **watcher:** --scan-commits closes the commit→finding loop (#243) (6519e16)
- **identity:** S1-a continuity_token deprecation wiring + clock-skew tolerance (#240) (7193b31)
- **watcher:** self-calibration loop — per-(pattern × file_class) precision floor with ε-greedy probe (#225) (2c33fea)
- **db:** ExecutorPool — asyncpg loop-isolation wrapper for anyio (P2 full) (#218) (0de7c7f)
- resident progress detection (phase 1 telemetry) — rebased (#206) (4ca6394)
- **vigil-hygiene:** Phase 1 dry-run branch sweep (#197) (d090eb0)
- restore Docker quickstart for one-command server bring-up (#193) (f5c2240)
- **identity:** X-Unitares-Operator header + is_operator_caller helper (#187) (16b6793)
- phase 5 — bootstrap-only observability query path + REST endpoint (#188) (abc81bc)
- phase 4 — SessionStart hook attaches initial_state for bootstrap check-in (#183) (a3c0cfc)
- phase 3b — get_agent_state_history exclude_synthetic + hydration filter (#178) (e4fc865)
- phase 2 — onboard.initial_state writes bootstrap check-in row (#171) (20a558e)
- phase 1 — synthetic column on agent_state for bootstrap check-ins (#168) (65c22ff)
- **identity:** auto-label embeds UUID[:8] suffix for operator disambiguation (fed5f7f)
- **setup:** CLI entrypoint with --apply/--json/--non-interactive/--proxy-url (b8e4303)
- **setup:** five-phase pipeline orchestration with idempotent apply (a54f865)
- **setup:** stdio MCP snippet generation for claude_code, codex, gemini, copilot (d998024)
- **setup:** MCP client detection from home directory (5755a6a)
- **setup:** anchor-dir and secrets-file scaffolding (--apply only, idempotent) (7919aef)
- **setup:** remediation generator for doctor fail/warn results (9fd7dfc)
- **setup:** doctor subprocess wrapper with strict JSON parsing (29f17f0)
- **setup:** module skeleton + bootstrap MCP-SDK import check (0a3a9cb)
- **s13:** identity() v2 fresh-instance gate + concurrent_session_binding_observed audit event (#156) (2781684)
- **watcher:** scope SessionStart findings to current worktree, demote others to footer (#152) (be7ff74)
- **identity:** same-host ppid consistency check (#128) (#151) (d5849bd)
- split doctor's HTTP check into TCP-listening + HTTP-responsiveness, bump timeout to 5s (7bad140)
- add unitares_doctor.py — diagnostic checklist for local + operator installs (b699c44)
- **identity:** S13 v2-ontology gate for arg-less onboard() (#148) (1c5b9e5)
- **llm_delegation:** pass reasoning.effort=none for qwen3.x (#137) (80a3180)
- **dashboard:** expose parent_agent_id + spawn_reason in list_agents, render lineage badges (#134) (abd7357)
- **identity:** S8a Phase-1 default-stamp class tags at onboard (#121) (d1804da)
- **dashboard:** Watcher panel — findings pipeline + pattern dismiss-rate (#116) (e5e5dbc)
- **chronicler:** DB-backed scrapers + dashboard clarity (#114) (6d108d0)
- **identity:** S6 follow-up — wire seed_genesis_from_parent into onboard (#112) (a8bf5d7)
- **identity:** S6 Option B — substrate-earned tier routing + lineage-seeded genesis (#109) (15a3559)
- **chronicler:** give Chronicler a governance identity + EISV check-ins (#108) (4424bc0)
- **identity:** S6 Option B — substrate-earned tier routing + lineage-seeded genesis (#107) (5304a9f)
- **identity:** invert resident_fork_detected semantics (S5) (#106) (ba69c63)
- **identity:** R4.1 — verify_substrate_earned operational check (#96) (0a1a560)
- **vigil:** resident tag-hygiene check + /v1/residents/tag_audit endpoint (#94) (06002d1)
- **grounding:** ecosystem fixes for silent class-baseline fallback (db1ed47)
- **identity:** PATH 2 IP:UA pin cross-check — Phase A (observation) (#92) (b14dba7)
- **metrics:** compact chart + second metric (tests.unitares.count) (#85) (71a5bb7)
- **call_model:** drop Gemini provider — router exposes only ollama + hf. GOOGLE_AI_API_KEY was never wired in plist/env, so the gemini branch only returned MISSING_CONFIG at runtime. Schema enum is now Literal['auto','hf','ollama']. Custom endpoints (provider='openai' etc) must pass an explicit model — no gemini-flash default. Closes #66. (#80) (26f182d)
- **identity:** PATH 1 fingerprint cross-check — Phase A (observation) (#83) (2b01218)
- **identity:** emit identity_hijack_suspected event from PATH 0 Part C gate (#78) (b7a31e1)

### Changed

- **lifecycle:** share resume persistence helper (#235) (093df20)
- **watcher:** split findings.py + _util.py out of agent.py (#119) (384aca6)
- **tests:** consolidate agent metadata + lifecycle tests (0f4ff59)

### Fixed

- **test:** lease-plane force-release contract test reads LEASE_PLANE_BEARER_TOKEN, not unset GOVERNANCE_TOKEN (#337) (5733f53)
- **sdk:** capture resident_name on identity() resume + GovernanceAgent passes name explicitly (#335) (770f9d9)
- **governance:** _resolve_agent_class consults agent_metadata cache for UUID-keyed residents (#332) (33010b3)
- **lease-plane:** route renew/3 with substrate through Repo, not LeaseHolder fast-path (#331) (2311d31)
- **error-handling:** order TIMEOUT before auth patterns so tool names containing 'session' don't miscategorize (#316) (0b71a96)
- **watcher:** suppress P005 on same-name resource-factory pass-through wrappers (#315) (7f1e6ab)
- **sentinel:** wrap forced-release alarm poll in run_in_executor + asyncio.wait_for (anyio mitigation) (#290) (9af40fa)
- **docs:** ignore vendored Elixir docs in health check (e302e06)
- **ship:** cap PR title at first line so long commit bodies don't break gh pr create (faa4810)
- **watcher:** P005 — drop false positives on acquire-then-try idiom (#288) (9a3679d)
- **lease-plane:** SAVEPOINT race-recovery in acquire_step + concurrent test (#283) (7d5efd9)
- **resident-progress:** exempt event-driven residents from candidate-rate smoke (a8773a3)
- **lease-plane:** Phase A PR 6 — emit lease.deprecation_marked + _migrated events (#277) (ec119b0)
- **lease-plane:** Phase A PR 5 — council BLOCK fixes from PR 1-4 stack review (#273) (70ed903)
- **silence:** suppress duplicate resident row alerts (3a3f871)
- **lease-plane:** polish — URL decode, IPv6 doc, plug 1.18 pin (#257) (cd50cb1)
- **updates:** preserve fresh api_key when PG insert succeeds but meta setup raises (#255) (2bdedfa)
- **grounding:** persist tags before in-memory mutation in stamp_default_class_tags (#254) (8ef19b1)
- **analysis:** guard stale sqlite reads (37ceee9)
- **lease-plane:** close council-found gaps before Elixir build (#250) (af1df76)
- **resident-progress:** per-resident heartbeat cadence (#248) (5190418)
- **db:** register legacy migration slots (7f03568)
- **dialectic:** accept conditions alias + early-fail on agrees+empty (#247) (38a1713)
- **identity:** S21-b items 5+6 — consolidate dual resolve_session_identity calls + core_agent_row_status auth gate (#245) (e14544c)
- **tests:** point test_db_utils at renumbered migration slots (#244) (e6852cd)
- **governance:** infer missing tool result kind (a7a07b8)
- **watcher:** tighten pin timeout verifier (c05a0cf)
- **watcher:** tune hook cadence and P004 guard (2f66fea)
- **lifecycle:** suppress cache hydration created events (0689008)
- **identity:** stabilize trust tier drift (fcc25e6)
- harden knowledge graph cleanup write path (c2f3790)
- **identity:** harden S21-b session resolution (92241e0)
- **knowledge:** preserve identity through KG writes (dd5a597)
- **identity:** stabilize diagnostic session binding (7d984dc)
- **sdk:** keep UDS resident anchors uuid-only (14c123d)
- **identity:** reject substrate token resumes before session lookup (7b1fbd4)
- **dashboard:** align resident check-in timestamps (2ba70b2)
- **event-detector:** order seed by last_activity_at so substrate-anchored agents survive ephemeral session flood (#233) (2be2eb6)
- **kg:** AGE backend get_discovery/update_discovery SQL fallback for SQL-only orphans (#223) (6976ffc)
- **db:** close council-found cancellation gaps in pool wedge fix (#230) (a2dfac4)
- **db:** null-before-close + ExecutorPool idempotency for wedged-pool recovery (#229) (7e324ea)
- **db:** move pool-recovery destroy log inside lock + add identity re-check (#228) (7b9e552)
- **monitor:** persist last_update across restarts; clamp NTP step-back; log DT_MAX saturation (#224) (9c37ce4)
- **migrations:** seed epoch 3 in core.epochs (021) (#227) (f9648d2)
- **ci:** restore README Status line for check_doc_drift (#222) (a12a72f)
- drop hardcoded home-path default in vigil_hygiene --repo (#216) (1aa6e72)
- **tests:** opt-in coverage + align CI with make test and README (#214) (19c6402)
- **self_recovery:** persist paused_at + lifecycle_event in quick_resume and operator_resume (#208) (6742322)
- **observe:** decision_history hydration + verdict_distribution + redundant-hydrate cleanup (#205) (1d7e420)
- persist runtime state in direct_resume + honest export envelope (#202) (30997dd)
- heal cold monitors fleet-wide via flag-and-drain hydration (#200) (f13c39d)
- **identity:** PATH 1 sync-path fingerprint cross-check (#192) (8c7e069)
- **kg:** UTC-normalize timestamps and pin writer label at write time (#179) (e3d9d05)
- **vigil:** align _SENTINEL_AUDIT_TRIGGERS with Sentinel's actual emit names (#186) (ef781ce)
- **kg:** semantic routing transparency, FTS AND default, scope clarity, provenance.source (#165) (#182) (81c4d09)
- **health-probe:** bounded parallel scan for redis_cache.keys (#180) (89f58bd)
- **identity:** drop display_name fallback from quick_reference.for_knowledge_graph (784aa86)
- **setup:** tighten dry-run exit_code, always emit 'applied' in JSON (final review feedback) (963769e)
- **setup:** escape backslashes and quotes in TOML snippet (review feedback) (9031542)
- **setup:** detect_clients uses is_dir(), not exists() — guards against ~/.claude being a regular file (b258d36)
- **identity:** drop fingerprint-only sticky cache for MCP transport (Vigil-siphoning regression) (#160) (f61ada0)
- **calibration:** stop aliasing accuracy=trajectory_health; surface real outcome-match accuracy or null (#159) (1932bd1)
- **health_check:** surface circuit_breakers in response (regression of resident CB visibility) (#158) (7246c90)
- **s8c:** persist spawn_reason on upsert_identity + ON CONFLICT propagation (#155) (13a5ff4)
- **sentinel:** stop double-writing findings to KG (#154) (cc2d9d8)
- **vigil:** preserve groundskeeper counts across non-audit cycles (#153) (4b33dce)
- **runtime_config:** remove dead risk_reject_threshold branches (587e5a5)
- **test:** anchor vigil-summary tests on real clock so cycles_24h/writes_24h stay stable in CI (#144) (7ddd7aa)
- **identity:** wrap onboard-pin redis ops in wait_for + fix stale resident timeout tests (#143) (526d6ff)
- **watcher:** truthful dashboard + audit-trail for resolutions (#145) (815687e)
- **test:** anchor vigil-summary tests on real clock so cycles_24h/writes_24h stay stable in CI (#142) (86de4f3)
- **audit:** track Postgres background write to prevent silent GC (c8d583d)
- heal 'uninitialized' monitor via DB hydration + atomic save (#138) (#141) (04c9218)
- **alerts:** silence 6h timezone-induced silent_critical flood and dedup lifecycle_paused emission (#122) (3ef1f2b)
- **watcher:** import PROJECT_ROOT onto sys.path before first `agents.*` import (#120) (d2276ad)
- **identity:** disambiguate Claude clients in UA detection (#115) (a8ac4c7)
- **identity:** REST path must honor token ownership to prevent session-key hijack (#110) (#111) (8652f58)
- **dashboard:** add EISV history backfill so chart populates on proxy polling path (#99) (273e67d)
- **identity:** retire IP:UA fingerprint pin as implicit resume path (#98) (4c27b36)
- **identity:** persist declared lineage on onboard(force_new=true) (#97) (9a58270)
- **residents:** stamp persistent+autonomous on onboard, not just persistent (#93) (e0b37d1)
- **identity:** PATH 1 fingerprint cross-check (Phase A) + anyio-asyncio guard (#84) (7218e33)
- **dashboard:** align breakpoints + responsive main gutter (#91) (25126d5)
- **dashboard:** Activity fills available panel height + mobile rules (#90) (5a52b76)
- **dashboard:** cap Fleet Metrics panel at 1400px to match Activity (#89) (e34434e)
- **dashboard:** cap Chart.js ResizeObserver grow-loop on Fleet Metrics (#88) (82ad89b)
- **dashboard:** apply dashboard theme to Fleet Metrics chart (#87) (82cb3b5)
- **anomaly:** fan out anomaly_detected audit entries per-agent (#86) (9135d6f)
- **epochs:** backfill core.epochs row for epoch 2 + regression guard (5fcafdc)
- **http:** allow /dashboard/fleet-metrics.js (was 404) + regression guard (#82) (2850a0d)
- **identity:** middleware PATH 0 gate also emits identity_hijack_suspected (#81) (5eda4e1)
- **dashboard:** polish Fleet Metrics panel + surface Chronicler scrape status (#79) (602bb23)

### Documentation

- **proposals:** BEAM footprint roadmap v0 — Read A (control plane → BEAM, intelligence plane stays Python) (#333) (dbceb5e)
- **watcher:** record P016 FP sweep 2026-05-04 — SDK envelope parsing (#329) (c69e94d)
- **r1:** bookkeeping — mark §4.1 shipped (#321), §4.3 in flight (#324) (#325) (20efb92)
- **lease-plane:** §7.13 v0.11 TENTATIVE — resident heartbeat surface + substrate-state separation from monitor_decision (3 review passes) (#319) (db07cbb)
- **lease-plane:** §7.2.8 + §7.2.9 RESOLVED v0.10 — payload-shape spec pinned by test, scheme grammar lint already shipped (#318) (ebac3ef)
- **lease-plane:** §7.5 RESOLVED v0.9 — Pi remote_heartbeat TTL 180s→1000s from measured Steward audit data (#317) (8574d7c)
- **plexus-scope:** add lease-plane source + migration rows to surface-ID table (2402877)
- **coherence:** correct C1 default and bounds in C(V) docstring (b5d3b7e)
- **lease-plane:** mark Phase A complete — Sentinel asyncpg CONCERN closed by PR #290 + production verification, §9 reconciliation closed by PR #304 (28/0/0) (#305) (f8c5229)
- **council-fixes:** wrong skill file + wildcard surface_id rows + missing proposals preflight (#303) (ab8f2cb)
- **proposals:** land plexus-scope.md + bidirectional reference from lease-plane RFC (#302) (5277fe2)
- **preflight:** expand single-writer surfaces — identity code + plan.md ledger (#301) (25dd741)
- **ontology:** R1 v3.3 amendment — council confirmation pass folds three operator decisions, six forcing items, four impl-row constraints, eight doc-text fixes (d446c1f)
- **ontology:** R7 BEAM coordination kernel — in-repo placement decision (024c961)
- **proposals:** RETIRE anima broker BEAM port v0 (operator decision post-v0.4) (#279) (fc0dd5e)
- **ontology:** add R6 episode-fork response-shape decision doc v2 (aaaff89)
- **ontology:** add R2 honest memory integration design doc v2 (b7789f2)
- **proposals:** RFC v0.4 — anima broker BEAM port (S6 spike-result fold-in, ambiguous) (#278) (d411485)
- **proposals:** RFC v0.3 — anima broker BEAM port (S1 spike-result fold-in) (#275) (63e4466)
- **ontology:** refresh May 1 status board (877601a)
- **proposals:** RFC v0.2.1 — anima broker BEAM port (spike scope rescope, pre-experiment) (#272) (fb35386)
- **proposals:** RFC v0.2 — anima broker BEAM port (Pi-side coordination kernel) (#265) (044b7ac)
- add machine RAIN recovery protocol (c83d6da)
- **plan:** S20.1b + S15-c shipped 2026-04-27 (#249) (8d2856f)
- record episode fork provenance distinction (0a8d5e7)
- cross-link RFC v0.4 ↔ beam-coordination-kernel ontology plan (03b64f5)
- **ontology:** clean R6 dogfood pass after lease-plane sync (15bc980)
- **ontology:** R6 H1-H5 dogfood pass — Mnemos session 2026-04-29 (0ef9ef6)
- **proposals:** RFC v0.3 — ack-pass complete, council-clean (6bb0d75)
- **proposals:** RFC v0.2 — surface lease plane (Elixir/OTP coordination kernel) (e4d4156)
- **ontology:** add candidate provenance envelope (#246) (6e22163)
- **ontology:** S21-b items 5 + 6 — WIP-PR #245 council-clean, awaiting merge (c22b653)
- **ontology:** S8a Phase 2 prep — day-6 corpus shape + threshold recommendation + reframed PR scope (d880c98)
- **ontology:** S21-b audit pass — items 7 + 8 closed, items 5 + 6 carry forward (355b121)
- add harness substrate plurality ontology plan (#242) (122cd02)
- name single-writer surfaces and add gh-pr-list check before touching them (#239) (d5f9d21)
- remove three superseded/abandoned proposals (1eb2e82)
- remove CASE_STUDY.md (translation-layer that deep-tech evaluators don't need) (4231275)
- **ontology:** S21 canonical lineage-decl gap metric (PR #226 followup) (3ac1eeb)
- **ontology:** fix dead session_cache.py path reference (00f0663)
- **ontology:** S21-a review pass 2 — adversarial review post-merge (684944b)
- **ontology:** S19 closed in status board + S21-a fix council review (2140440)
- **ontology:** surface S21 session-resolution bypass + ledger row (03d1a90)
- drop relocated answer_lumen_questions row from cross-machine-surface (9371cdf)
- drop internal docs/superpowers/ planning tree; strip dead refs (a807a22)
- revamp README — runtime-first framing, paper demoted to Related Projects (30a63c9)
- refined phase-5 evidence contract — spec + council reviews + implementation plan (#207) (ad8569d)
- close S16 audit write tolerance (#199) (3cd6c4e)
- close S17 redis pin deadlock row (#198) (4d752ae)
- **ontology:** mark S20.1a resolved (plugin#23 merged); note concurrent-ship collision (43a2eb0)
- **health:** clear all warnings — linter robustness + cross-repo prefixes + proposal-stage placeholders (8d3c2dc)
- **ontology:** S20.1a WIP-PR pointer + council-review summary (b5d77e6)
- **ontology:** S20 amendment 2026-04-26 — pre-implementation audit + S11-a row corrections (3225ee9)
- **ontology:** mark S11-a effectively closed (lint test shipped, sibling audit done) (2aa9c20)
- **ontology:** mark S11-a primary fix shipped (plugin@ad4dfef) (c81954e)
- **ontology:** S20 amendment — hook layer already shipped via PR #19; S20.0 answered (e8c7fdc)
- **ontology:** S20 client cache scope narrowing + S11-a skill text drift (7abaaea)
- **proposals:** PATH 1 sync-path fingerprint check + code review (b69fcd1)
- **proposals:** compute-meter v1→v2→v2.1 council-review chain (v2.1 status: ready-to-build) (521e429)
- **coherence:** cross-link legacy thermodynamic and canonical manifold forms (#190) (0a3fa7e)
- **proposals:** UUID leak audit — redaction-only approach insufficient (febadba)
- **proposals:** list_agents UUID redaction — proposal + council reviews (b1aca7e)
- **readme:** add citation block + moat-narrative line (credibility-surface framing) (11a8847)
- drop governance_core fold-back changelog/IP narrative from public files (6389c0d)
- **plan:** add S16 (audit-write fire-and-forget) + S17 (Redis-in-handler) tech-debt rows surfaced by S13 Watcher pass (1f8b116)
- **identity:** align user-facing copy with Part-C ownership-proof posture (#149) (a89ca01)
- **ontology:** add scheduled-re-reads section for date-gated triggers (a24b511)
- **ontology:** review pass — S8c+S14 rows, R1 v3.2 amendment, S1 scope correction (62113ef)
- **ontology:** record operator acceptances for R1, S1, S8a Phase 2 (7f71c02)
- spec for config hot-reload (option C — admin tools, no watcher) (4caf2e8)
- **ontology:** add S13 row — server-side complement of S11 (1092aec)
- **ontology:** v7-fhat spec v4 — narrow re-scope per council (2 classes, 2 targets, observer framing) (a22c075)
- **ontology:** mark S8a Phase-1 shipped as PR #121 (81ea6f5)
- **ontology:** v7-fhat spec v3 + paper-positioning retention note (84a07ed)
- **ontology:** v7 $\hat{F}$ spec v2 — pre-registered parameterization + predictive horse race (80deb5d)
- **ontology:** v7 $\hat{F}$ spike spec — path (d) for honest FEP grounding (f72f604)
- **ontology:** WIP-PR convention + handoff pre-flight (pre-dispatch PR-scan checkpoint) (856e28b)
- **ontology:** S8a tag-discipline audit — findings doc (#113) (56a5577)
- **readme:** correct requirements-core vs -full split (#104) (aad5b3e)
- delete CONTRIBUTING.md, inline the load-bearing bits into README (#103) (1502855)
- **readme:** fix dead #compiled-dependency anchor → #core-engine-dependency (#102) (35c9cb5)
- hero SVG + CONTRIBUTING fixes (stranded from PR #100) (#101) (c23a22f)
- **readme:** defer EISV jargon, add try-it snippet, honest denominators (#100) (83a1d5a)
- **ontology:** refresh recommended-priority snapshot post-session (S11/R3/R4.1 shipped; new top: S5/S6/v7/PR-scan checkpoint) (8640a43)
- **ontology:** fold R3 worktree depth into appendix (call sites, open questions, unblocking table, re-scope observation) (9042bea)
- **ontology:** S11 resolved via plugin#17 + dogfood-lesson appendix (parallel-PR pattern recurred at execution) (c1e4554)
- **ontology:** R3 trust-tier annotation pass appended to plan.md (31d30d9)
- clear remaining dead refs (R4 landed inline; observability handler refs split for parser; retrieval_eval README repointed to shipped state) (4033205)
- **ontology:** repoint S11 plan refs (s11-consolidation.md folded into plan.md; plugin scripts disambiguated) (eee2488)
- clear dead refs — skip .worktrees/ and historical planning dirs in check_doc_health (66fb06d)
- **readme:** signpost the three AI-CLI bootstrap files at root (907cebb)
- **ontology:** operational pattern appendix — how to turn plan into action (56842cd)
- **ontology:** S11 consolidation audit — 5 branches already merged, recommend synthesized PR (3e66766)
- **ontology:** R4 v1 draft + Q3 resolution (v7 thesis accepted) (9847cd9)
- **ontology:** S11 reality check — 5 WIP branches exist (9a40b46)
- **ontology:** S11 — revise default session-start behavior (the teeth) (31a9148)
- **ontology:** A2+A3 audit findings appended to plan.md (80a3def)
- **ontology:** paper-positioning note (A1 — answers Q3) (7e5da10)
- **ontology:** identity taxonomy + resolution plan (e22bae8)

### Tests

- **r6:** close design-doc test plan items 10+11 — thin shape + is_fork invariant across all R6 v2 cases (#308) (b8d06ed)
- **lease-plane:** close §9 residual — alias + force-release token rejection (28/0/0) (#304) (50df44b)
- **lease-plane:** close 2 §9 Elixir router rows (held_by_other 409 + surface_kind ignored) (#296) (059d7e0)
- **lease-plane:** close 2 §9 storage-layer rows (CHECK + generated column) (#294) (f063b94)
- **lease-plane:** close 3 more §9 named-gate rows for AcquireRequest + canonicalize (#293) (221593a)
- **lease-plane:** close 4 §9 named-gate rows for AcquireRequest + held_by_other (#292) (061e57c)
- **lease-plane:** property-based tests for Canonicalize (#282) (b72f9bb)
- **resident-progress:** include never-seen status in endpoint allow-set (#262) (271a368)
- **kg:** update AGE get/update_discovery tests for SQL fallback contract (#232) (0bede6e)
- **s19:** skip read_service_label tests on non-darwin (#191) (15586d2)
- **substrate:** mock sys.platform=darwin in launchctl test fixture (#185) (6a357a2)
- **setup:** lock in dry-run exit_code and JSON 'applied' field invariants (deb4151)
- **identity-v2-redis:** mock time.time instead of sleeping (144d0ad)

### Other

- pin-TTL bleed SQL + report script for PR #203 (#312) (7274386)
- **lease-plane:** launchd plist template + start.sh wrapper for persistent service (#307) (cfefd10)
- migration-drift renumbering + apply plan (no DDL run) (#236) (34ab4d7)
- **residents:** pills click to dedicated panels (Vigil/Sentinel/Watcher/Chronicler), rp-row for Steward/Lumen/etc, agent card as fallback (e348b6b)
- bump CURRENT_EPOCH 2→3 (broadened tactical truth channel — already applied via bump_epoch.py to live DB) (#220) (e8291ad)
- backfill fixes — drop epoch filter, COALESCE confidence keys, feed CalibrationChecker, flush postgres write; handler forwards per_channel_* keys (#219) (a9f625f)
- cap vigil + sentinel panel width at 1400px to match peer panels (#217) (80536d2)
- broaden tactical truth channel + per-channel surface (epoch 2→3) (#215) (e49d9ea)
- default chronicler chart to 14-day window (4d09d91)
- ambient aurora + grain overlay + HUD corner brackets (#212) (04e1629)
- remove dead unitares/hooks/ directory; salvage session_end_stash to scripts/dev/ (#210) (af8e16a)
- honest absence when tactical signal is starved (#201) (d70e072)
- identity resolution observation event for pin-TTL analysis (#203) (b4c5d4b)
- stop labeling trajectory health as calibration (91ac421)
- Agents grid reflects lifecycle WS events live (no more 30s tick lag) (7cd0155)
- surface last_point_ts in /v1/metrics/catalog so Chronicler badge avoids N+1 (1dd956e)
- branch hygiene automation (Vigil sweep + conditional webhook) (#196) (c6beeb4)
- filter Chronicler .error twins, surface active failures via panel badge (721e2f6)
- **ops:** launchd template for com.unitares.dep-sweep — Sunday 09:00 weekly Homebrew + Pi outdated-package report (597d4e3)
- **dev:** weekly dep-sweep — Homebrew + Pi outdated package report (com.unitares.dep-sweep launchd target) (99b29f4)
- extend P005 carve-out to <var>=None + try/finally pattern (#174) (e7667f3)
- aggregate GitHub traffic for CIRWEL org (66dca5e)
- bootstrap-checkin §3.5 — use core.substrate_claims registry (c98d1d1)
- resident progress detection (phase 1) implementation (0c6e15b)
- onboard-bootstrap-checkin v2.1 + council reviews (81442f2)
- stamp WIP-PR #164 on row (PR1: schema + enroll CLI) (c93e40d)
- resident progress detection (phase 1) design (3d0784d)
- ship council review provenance (code-architect + adversary) (44ff654)
- open resume-time substrate attestation row (Hermes-incident-driven; B-strict preferred) (10c3c97)
- drop P005 false-positives on context-managed acquire() (759681f)
- unitares setup wizard — 10-task TDD implementation plan (b83ee09)
- revise unitares setup — stdio is already implemented (mcp_server_std.py), drop rejection-flag bridge (a33d72c)
- unitares setup script — guided install wizard, council-reviewed (76dbc54)
- ratify Option A + mark §6 cure and S15-a shipped (f168e3c)
- **plist:** default UNITARES_LLM_MODEL to qwen3.6:27b-coding-nvfp4 (d3ebb21)
- add dedicated Vigil panel + hide its writes from main feed (#140) (f172399)
- resident-agent template polish (cycle timeout, hooks, docs) (#136) (06eb002)
- continuity_token retirement plan doc (Track C C1 complete) (9b43cf2)
- design doc for verify_lineage_claim (824b177)
- log per-phase latency in process_agent_update workflow (#133) (d3c6da4)
- concurrent identity binding invariant — audit-only v1 (#123) (#126) (472763b)
- switch governance-mcp to postgres knowledge backend (4a36953)
- add one-line UNITARES pitch + bump paper ref to v6.9.1 (2d8feb0)
- bump paper reference to paper-v6.9 (ea93b43)
- bump paper reference to paper-v6.8.2 (330fbe5)
- add IPv6 loopback proxy so cloudflared 2026.3+ WS upgrades reach uvicorn (bba6009)
- bump paper-v6.8 -> paper-v6.8.1 (2cd921a)
- bump health-snapshot fixture version to 2.12.0 (917a5a8)
- consolidate on scripts/ops/version_manager.py; strip dead refs; pyproject triggers VERSION check (cb74860)
- anchor-resilience series (3 PRs, detector + rotate script + SDK guard) (#77) (40882dd)
- committed rotate-secrets.sh with surgical anchor strip (#76) (75147c2)

---

## [Unreleased]

---

## [2.12.0] - 2026-04-20

### Added

- **KG retrieval rebuild — Phase 1 / eval harness** (#56) — retrieval evaluation harness plus a 20-pair seed corpus and baseline pin under `tests/retrieval_eval/`. Locks in V0 metrics so later phases have a reference point. (`scripts/eval/retrieval_eval.py`)
- **KG retrieval rebuild — Phase 2 / BGE-M3 embedder** (#58) — new embedder selectable via `UNITARES_EMBEDDING_MODEL=bge-m3`. Keeps the previous model as default; flip per-deployment. BGE-M3's 8192-token budget is what motivates the Phase-6 embed-window widening below.
- **KG retrieval rebuild — Phase 3 / cross-encoder reranker** (#60) — opt-in reranker infrastructure, default off. Scores the filtered pool after fusion when enabled.
- **KG retrieval rebuild — Phase 4 / hybrid RRF fusion** (#62) — RRF fusion of BM25 + dense retrieval behind `UNITARES_ENABLE_HYBRID`. Adds `full_text_search()` on the AGE store, switches `ts_rank` → `ts_rank_cd`, and treats tags as an RRF boost rather than a hard filter. `rrf_scores` surfaced alongside `similarity_scores` / `rerank_scores`. Compose order when both Phase 3 and Phase 4 are on: hybrid fuses first, then rerank scores the filtered pool.
- **KG retrieval rebuild — Phase 5 / 1-hop typed-edge graph expansion** (#63) — behind `UNITARES_ENABLE_GRAPH_EXPANSION`. After RRF fuse, pull 1-hop neighbors (`related_to` / `responses_from` / `response_to`) from the top-10 seeds at discounted score, hydrate missing neighbors via `graph.get_discovery` (capped at 30 DB trips). Tag-filter guard extended to all `hybrid_rrf*` modes. Infrastructure only — the 20-pair seed corpus is too sparse to move the needle today; payoff scales with corpus size and response threading.
- **KG text limits raised** (#73) — write caps 1000/5000 → 4000/20000 chars (summary/details); embed-text window 500 → 6000 chars (closes a quiet retrieval gap where details past char 500 were dead weight for semantic search); read-side preview 100 → 500 chars with `has_more_details` and `details_length` hints so agents can decide whether to round-trip for the full body. Limits consolidated in `src/mcp_handlers/knowledge/limits.py` (previously duplicated inline in three handler call sites). Existing discoveries keep their legacy embeds until edited; backfill is out of scope.
- **Fleet metrics substrate** (#68) — Postgres-backed catalog-gated time-series store. Single `metrics.series (ts, name, value)` table; dotted names (`tokei.unitares.src.code`); writes gated by an in-code catalog (`src/fleet_metrics/catalog.py`) so a leaked bearer token cannot inject arbitrary series names. Three bearer-authed endpoints: `POST /v1/metrics`, `GET /v1/metrics/series`, `GET /v1/metrics/catalog`.
- **Chronicler resident** (#71) — daily scraper agent populating fleet metrics via the catalog.
- **Fleet Metrics dashboard panel** (#75) — catalog-driven time-series line chart (Chart.js 4.4 + `chartjs-adapter-date-fns`, both already loaded). Dropdown auto-populates from the catalog; polling on demand (refresh button + dropdown change); empty-state rendering for series with no points yet. Completes the three-PR track (substrate → Chronicler → dashboard).
- **Resident-fork detector** (#70) — when onboard detects a label collision with an agent carrying the `persistent` tag, log at WARNING and emit `resident_fork_detected` via the broadcaster. Rename behavior preserved (fork still completes; onboard is not blocked) but the fork now announces itself instead of absorbing silently. Closes the detection-gap blindspot surfaced in the 2026-04-19 anchor-resilience council review. Adds `db.agent_has_tag` on `AgentMixin`. Phase 1 of 3.
- **SDK `refuse_fresh_onboard` opt-in for residents** (#74) — residents can refuse a fresh onboard, forcing the client to resume an existing identity instead of silently creating a new one.
- **Dashboard residents precedence** — `/v1/residents` now supports a third fallback: `KNOWN_RESIDENT_LABELS ∩ fleet` when neither `UNITARES_RESIDENT_AGENTS` nor `meta.resident=True` is set. Reuses the canonical list from `grounding/class_indicator.py` so operators don't have to duplicate it in plist env vars. Precedence: env → metadata flag → known-residents ∩ fleet → none. `_resolve_resident_labels` now returns `(labels, source)`.

### Changed

- **`ship.sh` runtime-path branch prefix is now agent-scoped** — `claude/auto/...` when `CLAUDECODE=1`, `codex/auto/...` otherwise (backward-compatible default); override with `UNITARES_SHIP_AGENT=<name>`. Rationale: multiple concurrent agents auto-ship to the repo; self-identifying branch names make the audit trail honest.
- **README** — paper reference bumped v6.7 → v6.8.

### Fixed

- **Identity Honesty Part C — strict-mode gates** (2026-04-18) — closes the three ghost-creation paths that PR #35 revert called out. One env flag (`UNITARES_IDENTITY_STRICT`) gates all three at their source instead of layering more archive/resurrect guards on top:
  - **PATH 0 bare-UUID resume** (`identity/handlers.py`, `middleware/identity_step.py`) now requires a `continuity_token` whose signed `aid` claim matches the requested `agent_uuid`. Prior behavior accepted any known UUID as proof of ownership — the mechanism behind "another agent resurrected a dormant agent from yesterday." Invariant #4 violation closed.
  - **Handler FALLBACK 2 ghost factory** (`support/agent_auth.py:214-221`) — `auto_<ts>_<uuid8>` IDs generated when the caller has no `agent_id` and no session binding. Gated on the same flag.
  - **Onboard-triggered orphan sweep** (`identity/handlers.py:1340-1349`) — removed. With ghost creation gated upstream, the nightly sweep in `background_tasks.py` is sufficient. This was the driver of "agent archived almost immediately."
  - **Modes:** `off` (emergency rollback), `log` (warn `[IDENTITY_STRICT]`, do nothing else — **default**), `strict` (reject with recovery guidance). Default `log` surfaces the magnitude of the problem via warnings without breaking any caller; operator flips to `strict` after external-client audit.
  - **Residents updated:** SDK `GovernanceAgent` and Watcher now load their saved `continuity_token` into the client before the PATH 0 resume call so they keep working in strict mode without anchor-file schema changes.
  - **Follow-up (out of scope this PR):** audit external clients (Codex plugin, Pi/Anima, Discord bridge, dashboard, raw REST callers); flip default `log → strict`; delete the dead bare-UUID / FALLBACK 2 / onboard-sweep code paths.
- **`call_model` — empty content fallback to `message.reasoning`** (#72, partial #66) — gemma4 / deepseek-r1 via Ollama's OpenAI-compat adapter split the final answer and thinking trace into separate fields; a truncated reply left `content=""` and hid the whole response. Now falls back to `message.reasoning` when content is empty.
- **`call_model` — stop silently rewriting `llama-3.1-8b` → `llama3:70b`** (#69, partial #66) — local auto-default now honors `UNITARES_LLM_MODEL` (`gemma4:latest` fallback); recovery hint points at `ollama list` instead of stale model names.
- **Dialectic reviewer auto-select gated** (#65) — behind `UNITARES_AUTOSELECT_REVIEWER` (default off). The candidate pool is ghost sessions + non-reasoning scripts, so auto-assign was dishonest until a real summonable reasoner is wired in. Callers already handle `None` cleanly (self-review / awaiting-facilitation / `NO_REVIEWER`).
- **Stuck-recovery self-review fallback closed** (#59) — `_trigger_dialectic_for_stuck_agent` now passes `agent_metadata` + paused tags to `select_reviewer`. Prior behavior called it without metadata, `select_reviewer` returned `None`, and the caller's self-review fallback assigned the paused agent as its own reviewer on every sweep — producing dozens of doomed `reviewer: 9a6681ec...` sessions for Steward. When no peer is eligible, return `None` and let `auto_initiate_dialectic_recovery` own the LLM-assisted fallback for single-agent deployments.
- **`/api/events` int-cursor replay** (#67, closes #25) — dropped UUID audit rows from `/api/events` when clients pass `?since=N`. They were unreachable via the int-cursor protocol and replayed on every poll. Dashboard (no `since`) still sees them.
- **Loop-detect Pattern 4 pause branch freshness guard** (#61) — `PAUSE_LOOP_FRESHNESS_SECONDS=3600`. Pattern 4's pause branch counted `pause` entries in the last 10 decisions with no time window; once it fired, every subsequent update was rejected before it could be recorded, so the pause-heavy window never rolled over and the agent was locked out indefinitely on a static history. Seen in production: Steward (`9a6681ec`) flapped for 13+ hours on a frozen meta. Unparseable timestamps fall back to firing so bad metadata can't silently suppress a real loop.
- **Loop-detect Pattern 4 / Pattern 7 proceed-branch freshness guard** (#57) — `PROCEED_LOOP_FRESHNESS_SECONDS=600`. The 10-proceed window had no floor on how old the newest timestamp could be, so a dormant agent whose last proceed burst happened days ago kept re-firing. Seen in production as a stale 2026-04-17 burst on `2aa0ec9e` re-flagging itself on 2026-04-20.
- **KG search noise-floor fallback removed** (#55) — removed the 0.2-threshold fallback; surface scores unconditionally.

### Removed

- **Neighbor coupling** (2026-04-17) — deleted `AdaptiveGovernor.apply_neighbor_pressure` / `decay_neighbor_pressure` and the `neighbor_pressure` / `agents_in_resonance` state fields from `unitares-core`. Deleted `cirs.hooks.maybe_apply_neighbor_pressure`, `auto_emit_coherence_reports`, `_lookup_similarity` and all re-exports. The production call site has been disabled since the `phases.py:1005` comment landed; this commit removes the dormant scaffolding so the code reflects actual runtime behavior. Rationale: agent-to-agent threshold coupling undermined independent per-agent judgment and produced correlated EISV drift that confounded fleet anomaly detection. Forward-compatible: persisted `GovernorState` snapshots carrying `neighbor_pressure` keys continue to load (unknown keys ignored).

### Tests

- **`monitor_phi` / `monitor_calibration` unit coverage** (#64) — direct coverage lifted from 63%/60% to 100%. Task-type risk adjustment branches and trajectory/strategic/tactical calibration paths were previously exercised only transitively via `test_governance_monitor*.py`. No production changes.

---

## [2.11.0] - 2026-04-07

### Added

- **Sentinel agent** — continuous independent observer that monitors governance in real-time via WebSocket. Detects fleet-wide anomalies (coordinated degradation, entropy outliers, verdict shifts), correlates incidents across typed events, and generates template-based situation reports from the audit trail. Runs as a launchd-managed persistent service alongside Vigil. (`scripts/ops/sentinel_agent.py`)
- **Broadcaster event bus** — typed event emission (`lifecycle_*`, `identity_*`, `knowledge_*`, `circuit_breaker_*`) with a queryable in-memory ring buffer (2000 events, ~6h). Foundation for Sentinel and future dashboard consumers.
- **Behavioral baseline persistence** — Welford stats (mean, variance, count per signal) now persist to PostgreSQL (`core.agent_behavioral_baselines` table) via fire-and-forget async writes. Baselines survive server restarts instead of resetting.
- **KG confidence cross-check** — discovery confidence is clamped to `agent_coherence + 0.3` on write. Annotates provenance with `confidence_clamped: true` and broadcasts `knowledge_confidence_clamped` event.
- **Circuit breaker telemetry** — trip timestamp ring buffers on both governance and Redis circuit breakers. Exposed via `get_governance_metrics()` as `circuit_breakers` section with `trips_1h`, `trips_24h`, `last_trip`.
- **Trajectory drift alerts** — emits `trajectory_drift` audit event and `identity_drift` broadcast when lineage similarity drops below 0.6. Also broadcasts `identity_assurance_change` on trust tier transitions.
- **Agent silence detection** — background task (every 10 min) monitors persistent agents (Vigil, Lumen, Sentinel) for missed check-ins. Alerts at 2x expected interval, critical alert at 5x. Deduplicates alerts, clears on recovery.

### Fixed

- **CI doc drift check** — removed stale reference to deleted `docs/guides/NGROK_DEPLOYMENT.md` that caused `FileNotFoundError` in CI.

---

## [2.10.0] - 2026-04-04

### Breaking

- **Docker removed** — Docker Compose and `postgres-age` container retired. All services now run via Homebrew PostgreSQL@17 on port 5432 with AGE 1.7.0. Migration scripts and Docker-era docs cleaned up.

### Added

- **Process marker self-healing** — the HTTP server now recreates missing `data/.mcp_server.pid` and `.mcp_server.lock` markers while running, reducing false-negative local health checks after interrupted stop/start sequences.
- **Direct HTTP identity resolution** — the HTTP request layer now resolves bound identity from `client_session_id` or `continuity_token` before direct tools run, so request-scoped tools can reuse the same session continuity path as MCP callers.
- **Pydantic schemas** for `outcome_correlation` and `reassign_reviewer` tools.
- **Doc health checker** added to pre-push pipeline.
- **Grounded outcome study utilities** — added exogenous-vs-endogenous outcome classification, data-quality reporting, grouped regression helpers, and an analysis script for outcome-correlation validation.

### Changed

- **Behavioral EISV promoted to primary** — ODE dynamics demoted to diagnostic. Pattern analysis now uses behavioral EISV histories for trend detection.
- **Operator-facing state views** — dashboard, metrics, and persisted state now expose explicit `primary_eisv`, `behavioral_eisv`, `ode_eisv`, `ode_diagnostics`, and shared state semantics instead of overloading one flat EISV view.
- **Self-relative behavioral baselines** — per-agent Welford mean/std after ~30 updates; assessment uses z-score deviation from agent's own operating point instead of fixed thresholds. Absolute safety floors (E<0.30, I<0.30, S>0.70, |V|>0.50) always apply regardless of baseline.
- **Behavioral coherence for outcomes** — outcome events now feed behavioral EISV directly, closing the loop between governance verdicts and observable results.
- Renamed internal "DNA/genotyping" terminology to standard ML terms (behavioral baseline, warmup, self-relative scoring). Persistence backward-compatible with old `dna_stats` key.
- **HTTP boundary cleanup** — core read tools (`health_check`, `get_governance_metrics`) now use transport-neutral service/data helpers, and direct HTTP responses preserve multi-block/non-text MCP content instead of collapsing everything to the first text block.
- **Identity response shaping** — `identity()` and `onboard()` now consistently expose compact operator diagnostics (`session_resolution_source`, `continuity_token_supported`, `identity_status`, `bound_identity`) across the main and early-return paths.
- **Process update orchestration** — `process_agent_update` response assembly and workflow sequencing now live in dedicated service modules, reducing handler/transport coupling.
- **Ops scripts** — `start_unitares.sh` and `stop_unitares.sh` now wait for processes to exit and avoid unlinking marker files from a freshly restarted server.
- **Tool surface reduction** — `TOOL_MODE` enforced at FastMCP registration, reducing exposed tools from 40 to 20.
- **Trajectory identity aligned** with paper's six-component signature.
- **KG search** — default multi-term queries use OR; redundant FTS per-term fallback removed.

### Security

- **MCP listen defaults** — Default bind address is `127.0.0.1`. Opt in to `0.0.0.0` with `UNITARES_BIND_ALL_INTERFACES=1` or `UNITARES_MCP_HOST`. LAN/ngrok `Host` / Origin allowlists are no longer hard-coded in source: use `UNITARES_MCP_ALLOWED_HOSTS` and `UNITARES_MCP_ALLOWED_ORIGINS` (comma-separated). See `src/mcp_listen_config.py` and `CLAUDE.md`. LaunchAgent and `start_unitares.sh` set the previous bind-all + example allowlists for existing deployments.
- **Secrets removed from git** — Docker services bound to localhost.

### Fixed

- **Resident-agent resume safety** — strong continuity-token resume is now required for Vigil-style resident agents, and rejected identity-claim paths return structured errors instead of falling through into accidental forks.
- **Timezone mismatch in auto-archive** — `_auto_archive_ephemeral_agents` used `datetime.now(timezone.utc)` for cutoff but timestamps were stored as naive local time, causing agents in non-UTC timezones to appear hours older than reality and get instantly archived.
- **Content→details param mapping** — `content` parameter passed to `store_knowledge_graph` was not persisting to the `details` field due to Pydantic `model_dump()` populating all Optional fields as `None`, which defeated `dst_key not in arguments` guards in `action_router`.
- **Search NoneType crash** — `limit=None` from Pydantic caused `NoneType * int` error in search handler.
- **Identity continuity precedence** — explicit stable session continuity now wins over name-claim recovery, preventing `identity(client_session_id=..., name=...)` from silently jumping to an older named identity.
- **Durable stable session binding** — `onboard()` now persists the returned stable `client_session_id` through the normal session-bind path, so a Redis miss no longer causes a fresh UUID to be created on later resume.
- **HTTP work logging continuity** — `process_agent_update(client_session_id=...)` and `process_agent_update(continuity_token=...)` now resolve the correct bound identity on fresh HTTP requests instead of requiring the caller to pass the raw UUID manually.
- **HTTP metrics continuity** — `get_governance_metrics(client_session_id=...)` now resolves the real bound agent instead of materializing an unrelated auto-generated identity on the HTTP path.
- **AGE discovery durability** and drift checks hardened.
- **Tags preserved** on knowledge updates when omitted.
- **High-severity KG updates** and anonymous note writes tightened.
- **Dialectic reviewer selection**, Vigil signals, and convergence guidance fixes.
- **Lock timeouts** classified as system errors.
- **Missing agent rows** repaired during lazy persistence.
- **Pre-existing test failures** resolved (circular import in `_generate_contextual_reflection`, identity adapter name claim path).
- **Doc health drift** — stale file references, stale counts, and false-positive doc-health warnings cleaned up.

### Removed

- Docker Compose and `postgres-age` container setup.
- Completed migration scripts archived.
- Unused exceptions module.

---

## [2.9.0] - 2026-03-29

### Breaking — Epoch Bump (1 → 2)

- **DB epoch bumped to 2** — Behavioral EISV replaces ODE dynamics. Existing state data computed under the old model is incompatible. Old data (epoch 1) remains in the database but is excluded from active queries. All agents start fresh in epoch 2 on next check-in.

### Added — Identity & Session Management

- **`bind_session` tool** — bridges the identity gap between REST hooks (which onboard via curl) and MCP Streamable HTTP (which uses a different session key). One call at session start syncs both namespaces to the same agent.
- **Thread-based identity** with honest forking — agents acknowledge fresh sessions instead of falsely claiming continuity. Epistemic context feeds into EISV dynamics.

### Added — Behavioral EISV (Non-Embodied Agents)

- **Behavioral sensor EISV** — non-embodied agents (Claude, Cursor) get synthetic EISV seeds derived from behavioral signals (response latency, error rates, task complexity patterns) instead of physical sensors.
- **Behavioral trajectory identity** — trajectory fingerprinting for non-embodied agents using behavioral patterns rather than sensor readings.
- **Coherence differentiation** — reduced V damping, wired behavioral signals into coherence calculation, adaptive delta for basin edge detection.

### Added — Sensor EISV & Spring Coupling (Lumen)

- **Sensor spring coupling** in EISV ODE — Lumen's physical sensor readings (temperature, humidity, light) now couple into the ODE as spring terms, grounding dynamics in physical state.
- **Normalized spring coupling** by dimension range width for consistent cross-dimension influence.
- Hardened sensor EISV: clip to physical bounds, removed dead code.

### Changed — Soft Barrier Dynamics (governance-core)

- **Soft barrier replaces hard clamping** in EISV ODE — cubic barrier potential in `_derivatives()` smoothly repels state away from bounds (C² continuous, zero in interior). Hard `clip()` in integrators demoted to safety net. Preserves Jacobian continuity for contraction/Lyapunov analysis.
- **Barrier parameters** added to `DynamicsParams`: `barrier_strength=2.0`, `barrier_margin=0.05`. Margins scaled proportionally for S (×2.0) and V (×4.0) ranges.
- **Analytical Jacobian** updated with barrier diagonal terms in `stability.py`.
- **Removed redundant V bounds clip** in `governance_monitor.py` post-ODE block — barrier handles it; S floor and coherence recalc retained.

### Added — EISV Dynamics & Governance

- **State velocity feedback** — rate-of-change of EISV dimensions feeds back into dynamics, enabling faster response to rapid drift.
- **Adaptive lambda2** via `theta.eta2` — coherence damping on entropy is now state-dependent.
- **Coherence reports** auto-emitted for neighbor pressure detection in multi-agent scenarios.
- **Dialectic condition enforcement** at tier-1 with genesis reseed fix.
- **Closed feedback loops** — calibration deviation, ethical drift, and behavioral patterns now feed back into governance decisions via CIRS oscillation detection.
- **Persistent AdaptiveGovernor state** — governor parameters survive server restarts.
- Tuned thresholds for coding agents (beta_default 0.60 → 0.70), fixed recovery loop spiral.

### Added — Knowledge Graph

- **System version in discoveries** — `system_version` auto-populated in provenance at store time. Surfaced as top-level field in search results. Pre-v2.8.0 entries show `null`.
- **Staleness warnings** — KG search flags open entries >60 days old or 2+ minor versions behind current with `staleness_warning` per discovery.
- **Concept extraction** — background task automatically extracts concepts from agent check-ins and creates knowledge graph entries with spawned edges linking related discoveries.
- **Spawned edges** — knowledge graph entries can now track provenance via `SPAWNED_BY` edges and tag-based queries.
- **Pool guard improvements** — connection pool health checks prevent stale connections from corrupting KG operations.

### Added — Infrastructure

- **Database hygiene** — automated retention policy, batch queries, and periodic maintenance tasks.
- **Docker Compose** — top-level `docker-compose.yml` for one-liner setup of PostgreSQL+AGE and Redis.
- **Gateway MCP server** — simplified 6-tool proxy on port 8768 for weak external clients (Cursor, etc.).
- **LLM-assisted dialectic recovery** when no peers are available for review.
- **Agent baselines** persisted to PostgreSQL for cross-session calibration tracking.

### Added — EISV Analysis Tools

- **Monte Carlo basin estimation** (`scripts/basin_estimation.py`) — maps safe operating region by sampling 10K random perturbations and integrating forward via `compute_dynamics()`. Confirmed global attractivity under linear I-dynamics mode (100% convergence across full state space).
- **Contraction analysis verifier** (`scripts/contraction_analysis.py`) — numerically computes the EISV Jacobian (analytical + numerical cross-validation to 5×10⁻¹⁰), verifies all eigenvalues negative, optimizes diagonal contraction metric. Bare rate: 0.046, optimized rate: 0.113. All Theta values contracting (400-point sweep).
- **Compositionality metrics** (`scripts/compositionality_metrics.py`) — measures topographic similarity and region consistency of Lumen's primitive language. Supports real data from Pi SQLite DB or synthetic data mode for development.
- **Analysis test suite** (`tests/test_analysis_tools.py`) — 36 tests covering all three tools.
- `analysis` optional dependency group in pyproject.toml (scipy, matplotlib, editdistance).

### Changed — Major Refactoring (5-Phase Module Split)

Decomposed monolithic files into focused modules for maintainability:

1. **Phase 1**: Extracted `tool_descriptions` to JSON
2. **Phase 2**: Split `utils.py` into 5 focused modules
3. **Phase 3**: Split `agent_state.py` into 7 focused modules
4. **Phase 4**: Split `identity_v2.py` into 3 focused modules
5. **Phase 5**: Split `cirs_protocol.py` into 9 focused modules

Additional refactors:
- Extracted `mcp_server.py` and `governance_monitor.py` into focused modules
- Extracted background tasks, flattened `lifecycle_stuck`, renamed `response_formatting`
- **LazyMCPServer singleton** — deduplicated `_LazyMCPServer` into `shared.lazy_mcp_server`, Pydantic runtime validation, `ConnectionTracker` extraction
- Deleted dead code, organized scripts, decoupled transport, split `admin.py`

### Changed — First Check-in Guidance

- **Convergence guidance suppressed for early check-ins** — When `update_count ≤ 3`, EISV-derived guidance replaced with honest "Not enough data yet" message. Prevents misleading advice based on initialization defaults.
- **Restorative block suppressed for early check-ins** — Same threshold, prevents false complexity divergence alerts on new agents.

### Changed — Agent-Facing Response Trimming

- **`health_check` lite mode** (default `lite=true`) — returns only component status without nested info/stats blocks, reducing context window usage. Use `lite=false` for full diagnostic detail.
- **KG `health_check()` lightweight** — runs 3 COUNT queries instead of full `get_stats()` census. Eliminates per-agent/per-tag breakdowns from every health check. `get_stats()` unchanged for admin use.
- **Uninitialized agent verdict** — agents with zero check-ins now get `verdict: 'uninitialized'` with `guidance: 'Call process_agent_update to activate governance'` instead of a generic `caution` verdict.

### Fixed

- **Health check CI timeout flake** — mocked Pi connectivity in all health_check tests to prevent real network calls timing out in GitHub Actions.
- **`compute_equilibrium()` linear mode bug** — was using logistic quadratic formula regardless of I-dynamics mode, returning I*=1.0 instead of correct I*=A/γ_I≈0.85. Now checks `get_i_dynamics_mode()` and uses correct formula. Also includes `beta_complexity * complexity` term in S* and computes E* = αI*/(α + βₑS*) instead of E* ≈ I*.
- **Inverted `is_bad` default** and unguarded NaN in outcome scores
- **Coherence margin always "tight"** — str/float coercion and recovery tau noise fixes
- **Behavioral sensor E saturation** and adaptive margin baseline drift
- **Silent binding to archived agents** — prevented, with KG search degradation surfaced
- **Metadata loaded before orphan cleanup** on startup
- Inline validation for `discovery_type`, `severity`, and `response_to` in KG single-store and leave_note paths
- Preserved schema metadata (descriptions, enums) in MCP tool aliases
- Missing `sanitize_agent_name` function in validators
- `anyOf` JSON schema handling in wrapper generator

### Docs

- Discord summoner design and implementation plan
- README rewrite with production validation data, architecture diagrams, and figures
- CLAUDE.md project instructions
- Archived completed plans, fixed stale doc references

## [2.8.0] - 2026-02-26

### Added — Dashboard Redesign

Major dashboard overhaul — Alpine.js + htmx interactive architecture replacing static HTML.

- **Slide panel component** with agent detail view, EISV trend charts, and quick action buttons
- **Alpine.js identity store** with "Me mode" — AI agents see themselves highlighted in the dashboard
- **Hash-based router** for deep-linking to agents, sessions, and discoveries
- **Scoped search** with agents/all toggle and debounced input
- **Keyboard shortcuts** (vim-style navigation) with tooltip directive and help store
- **Loading skeletons** and smart empty states for all data-fetching panels
- **Error handling wrapper** and connection health checker
- **Expandable agent cards** with accordion, inline IDs, and hover actions
- **Bold visual redesign** — accent-tinted cards, colored EISV values, stronger badges, panel identity
- **help.json** terminology database for contextual tooltips
- **EISV history + incidents endpoints** for htmx fragment rendering
- **Event IDs + `?since=` cursor** on `/api/events` for Discord bridge polling
- **Discovery expand-on-click** with expandable details in list items
- Removed redundant Activity Timeline panel (duplicated Agents list)
- Removed Lumen sensor panel from governance dashboard

### Added — Outcome Events Infrastructure

- **Outcome event tracking** for EISV validation — correlate governance state with actual results
- **Auto-emit outcome events** from check-ins, enabling calibration feedback loops
- `outcome_event()` tool for manual event recording

### Added — Stuck Agent Improvements

- **Cross-referencing** stuck agents with recent activity across knowledge graph
- **Unstick action** in dashboard with one-click recovery button
- **Zombie prevention** — prevent archived agents from resurrecting via auto-resume
- **Auto-unarchive** on onboard reconnect for legitimately returning agents
- **Dedup recovery notes** in knowledge graph to reduce noise

### Added — Knowledge Graph Lifecycle

- **Ephemeral notes** with automatic expiration
- **Periodic cleanup** for stale KG entries
- **`last_referenced` tracking** for discovery recency
- **Improved search UX** with better result formatting

### Added — Developer Documentation

- Developer guide for repo protection from agent damage
- Dashboard redesign design document and implementation plan
- Unified DB architecture design doc and implementation plan

### Changed — Streamable HTTP Migration

- **Primary transport migrated from SSE to Streamable HTTP** (`/mcp/` endpoint)
- Removed dead SSE code paths from server and pi_orchestration
- Network trust bypass for HTTP auth (local network clients)
- System health metrics added to dashboard

### Changed — Dialectic Protocol Hardening

- **Simplified convergence** — `agrees=True` resolves directly, no fourth phase needed
- **UUID alignment** via `require_registered_agent` — dialectic sessions use onboard identity
- **Synthesizer attribution** displayed in session transcripts
- **Reviewer auto-persist** and AGE tag normalization
- **Reviewer auto-assign** fix for empty string default
- **Mediator hijack prevention** (audit round 2)
- **`finalize_resolution` fallback** to thesis `root_cause` when synthesis missing
- **Ownership check removed** from archive/delete — dashboard can manage sessions
- **Agent UUID → label resolution** in dialectic panel
- Removed dead convergence code, fixed `agrees` coercion
- 7 correctness bugs found and fixed via protocol audit
- `DialecticDB` detects closed pool and auto-refreshes from backend

### Changed — Identity Consolidation

- **Unified `derive_session_key()`** — consolidated 6 separate derivation sites
- Removed deprecated `_derive_session_key` (underscore-prefixed)
- Fixed `get_bound_agent_id` import (identity_shared, not identity)
- Clarified agent_id vs UUID terminology in comments and docs
- Updated AGI-forward spec for identity_v2 current state
- Fixed identity churn, display dispatch, and tag normalization (audit)

### Changed — Lifecycle Module Refactoring

- **Split `lifecycle.py` monolith** — extracted `lifecycle_resume.py` (142 lines) and `lifecycle_stuck.py` (557 lines) into focused modules
- Lifecycle handler reduced from ~720 lines to focused orchestration

### Changed — Dashboard Performance & Accessibility

- **CSS containment** for render optimization
- **`requestAnimationFrame`** for chart updates, EISV chart capped at 60 data points
- **Debounced search** inputs, removed duplicate listeners
- **CONFIG object** with timing constants, replaced magic numbers
- **Semantic color CSS variables** and section headers for CSS navigation
- **ARIA labels**, modal roles, skip link, focus management, and focus trap
- Fixed heading hierarchy for perfect a11y score
- Removed 4 unused component classes
- JSDoc added to core dashboard functions

### Changed — Database & Backend

- Removed SQLite backend and dual-write entirely (complement to v2.7.0 cleanup)
- `normalize_tags` for knowledge graph entries
- Connection pool release and `acquire_compat` safety
- Cross-event-loop DB corruption fix with system health tracking
- Batch update counter persistence to prevent DB contention
- Update count regression prevention on server restart

### Fixed

- **httpx client leak** in `pi_orchestration` — unclosed async client
- **datetime shadowing bug** broke dashboard agent listing
- **False-positive stuck agent detection** and dashboard noise eliminated
- **Leave_note limit** raised from 500 → 6,000 chars, split into summary+details
- **Reject out-of-range** complexity/confidence, guard `observe(compare)` NoneType
- **Knowledge `content→details` param_map** for store action (alias fix)
- **Self-recovery** — allow archived agents to self-restore via `self_recovery(quick)`
- **UUID validation + auth checks** on dashboard fragment endpoints
- 7 dogfood friction issues from Sonnet 4.6 Web session
- 15 audit fixes + 8 audit round 2 fixes
- 3 CI failures (async calibration tests + observability mock)
- 2 pre-existing test failures resolved
- Async metadata loading and handler improvements

### Removed

- SQLite backend and all related test files (final cleanup)
- Dead SSE transport code
- Lumen sensor panel from dashboard
- Activity Timeline panel (redundant)
- 4 unused dashboard component classes
- Dead convergence code from dialectic protocol

### Tests

- 5,654 tests collected, 80% coverage target
- Test updates for async metadata, dialectic phase, KG truncation limits

---

## [2.7.0] - 2026-02-20

### Added — CIRS v2 Resonance Wiring
- **AdaptiveGovernor** PID controller: phase-aware tau/beta thresholds, oscillation detection (OI + flip counting)
- **Resonance → CIRS protocol loop**: `maybe_emit_resonance_signal()` emits RESONANCE_ALERT / STABILITY_RESTORED on state transitions
- **Neighbor pressure**: `maybe_apply_neighbor_pressure()` reads peer resonance alerts, applies defensive threshold tightening via coherence similarity
- **`was_resonant` tracking**: GovernorState tracks previous cycle for transition detection
- 13 new tests (3 governor + 4 signal + 5 pressure + 1 integration), 6,407 tests total at 80% coverage

### Changed — I-Channel Dynamics (v5 Paper Alignment)
- Default I-dynamics mode flipped from logistic to linear (`UNITARES_I_DYNAMICS=linear`)
- Linear mode prevents boundary saturation (m_sat = -1.23 under logistic), stable equilibrium at I* ≈ 0.80
- Auto-applies γ_I = 0.169 (V42P tuning) when using linear mode + default profile
- Dialectic protocol: added `design_review` session type with 7-day/30-day timeouts
- Dialectic protocol: self-review shortcut (single agrees=True sufficient when paused_agent == reviewer)
- Condition normalization: fixed to keep 2-char words, handle mixed-case, added "it"/"its" to filler list

### Changed — Database Architecture
- **Removed SQLite backend** — deleted `sqlite_backend.py` (1,116 lines), `dual_backend.py` (697 lines), and test files (2,299 lines). PostgreSQL is the sole backend.
- Removed `DB_BACKEND` environment variable — no more sqlite/postgres/dual switching
- Simplified `db/__init__.py` to always return `PostgresBackend`
- Removed SQLite paths from `audit_log.py`, `calibration.py`, `mcp_server.py`, `mcp_server_std.py`
- Total: **4,697 lines deleted**, 79 lines added

### Changed — Version Governance
- Bumped all version references from 2.6.x to 2.7.0

---

## [2.6.4] - 2026-02-08

### Added — KG Search Bias Fixes

Knowledge graph searches were biased toward old, heavily-linked philosophical entries from Dec 2025.
New agents would reflect on them, adding more links, creating a positive feedback loop. Four fixes:

- **Temporal decay** — 90-day half-life applied to blended search scores. Old entries still surface
  if semantically relevant, but don't dominate by default.
- **Status-aware scoring** — Archived entries scored at 0.3x, resolved at 0.6x, disputed at 0.5x.
  Open entries unaffected.
- **Connectivity dampening** — Capped effective connectivity input at 50 (was unbounded). Prevents
  heavily-linked entries from monopolizing search results.
- **Default archived filtering** — `semantic_search()` excludes archived entries by default.
  Callers can opt in with `include_archived=True`.
- **SUPERSEDES edge type** — New AGE edge for marking entries that replace others. Superseded
  entries get halved connectivity scores. Available via `knowledge(action='supersede')`.

### Fixed — CI Test Failures (55 tests)

- **test_model_inference.py** (45 failures) — `openai` package not in CI dependencies. Fixed by
  ensuring `OpenAI` attribute exists on module for patching. Skip `TestCreateModelInferenceClient`
  when `openai` not installed.
- **test_auto_ground_truth.py** (10 failures) — Fragile module-reload mocking broke when run
  alongside other tests. Root cause: `import src.X as Y` resolves via parent package `__dict__`,
  not just `sys.modules`. Fixed by patching both `sys.modules` AND parent package attributes.

### Tests
- 6,344 tests passing, 80% coverage

### Files Changed
- `src/storage/knowledge_graph_age.py` — Temporal decay, status multiplier, connectivity cap, SUPERSEDES edge
- `src/mcp_handlers/knowledge_graph.py` — Default archived filtering, supersede action
- `tests/test_model_inference.py` — CI fix for missing openai package
- `tests/test_auto_ground_truth.py` — CI fix for module import resolution in mocks

---

## [2.6.3] - 2026-02-06

### Changed — Dialectic Audit & Cleanup

- **Fixed 16 misleading `sqlite_*` import aliases** → `pg_*` across 3 dialectic handler files
  (backend has been PostgreSQL-only since Feb 2026)
- **Made `llm_assisted_dialectic` reachable** via `request_dialectic_review(reviewer_mode='llm')`
- **Consolidated `get_dialectic_session` + `list_dialectic_sessions`** into
  `dialectic(action='get/list')` via action_router — 31 → 30 registered tools
- **Implemented EISV governance update** from Pi anima sensor sync:
  `pi(action='sync_eisv', update_governance=true)` now feeds sensor state into governance engine
- **Removed dead SSE code** — 3 deprecated functions (~80 lines) from mcp_server.py
- **Fixed stale comments/metadata** across tool_schemas, admin, tool_modes, tool_stability

### Tests
- 2,602 tests passing, 0 failures, 49% coverage

### Files Changed
- `src/mcp_handlers/dialectic.py` — pg_ aliases, LLM reviewer, register=False for get/list
- `src/mcp_handlers/dialectic_session.py` — pg_ aliases
- `src/mcp_handlers/dialectic_reviewer.py` — pg_ aliases
- `src/mcp_handlers/consolidated.py` — Added dialectic action_router
- `src/mcp_handlers/pi_orchestration.py` — EISV sync governance update
- `src/mcp_handlers/tool_stability.py` — Dialectic aliases, stability tiers
- `src/tool_schemas.py` — Dialectic consolidated schema, LLM enum, stale refs
- `src/tool_modes.py` — Dialectic categorization update
- `src/mcp_server.py` — Removed dead SSE code

---

## [2.6.2] - 2026-02-06

### Changed — Architecture Refactoring (4 Refactors)

Four internal refactors to reduce boilerplate, improve clarity, and make the tool system
more maintainable. No breaking changes — all 30 tools, aliases, and behaviors preserved.

#### Refactor 1: Unified ToolDefinition Registry
- **Replaced 4 separate dicts** (`_TOOL_REGISTRY`, `_TOOL_TIMEOUTS`, `_TOOL_DESCRIPTIONS`,
  `_TOOL_METADATA`) with a single `ToolDefinition` dataclass + `_TOOL_DEFINITIONS` registry
- Backward-compatible accessor functions preserved (`get_tool_registry()`, etc.)
- **File:** `src/mcp_handlers/decorators.py`

#### Refactor 2: Declarative Action Router
- **New `action_router()`** function creates consolidated tools from `actions: Dict[str, Callable]`
- Supports `default_action`, `param_maps` (per-action parameter remapping), and `examples`
- Rewrote all 7 consolidated handlers (knowledge, agent, calibration, config, export, observe, pi)
- **`consolidated.py` reduced from 479 → 245 lines** — no more if/elif chains
- **Files:** `src/mcp_handlers/decorators.py`, `src/mcp_handlers/consolidated.py`

#### Refactor 3: Dispatch Middleware Pipeline
- **Extracted `dispatch_tool()`** from 440-line monolith into 8 composable middleware steps
- Each step: `async (name, arguments, ctx) → (name, arguments, ctx) | list[TextContent]`
- Steps: resolve_identity → verify_trajectory → unwrap_kwargs → resolve_alias →
  inject_identity → validate_params → check_rate_limit → track_patterns
- `DispatchContext` dataclass carries state between steps
- **`dispatch_tool()` reduced from ~440 → ~50 lines**
- **Files:** `src/mcp_handlers/middleware.py` (NEW), `src/mcp_handlers/__init__.py`

#### Refactor 4: Response Formatter Extraction
- **Extracted response mode branching** (auto/minimal/compact/standard/full) from `core.py`
- `format_response()` function handles all mode filtering and context stripping
- **~190 lines removed from `core.py`**, replaced with 10-line function call
- **Files:** `src/mcp_handlers/response_formatter.py` (NEW), `src/mcp_handlers/core.py`

### Added — UX Friction Fixes (v2.6.1 session)
- **Dashboard overhaul** — live EISV sparklines, dialectic timeline, trust tier badges
- **Name-based identity resolution** (PATH 2.5) — agents reconnect by name, not session key
- **Observe tool fix** — `target_agent_id` supports labels, proper schema

### Tests
- 2,194 tests passing, 0 failures, 43% coverage

### Files Changed
- `src/mcp_handlers/decorators.py` — ToolDefinition dataclass + action_router
- `src/mcp_handlers/consolidated.py` — Rewritten with action_router (479→245 lines)
- `src/mcp_handlers/middleware.py` — NEW: 8-step dispatch pipeline
- `src/mcp_handlers/__init__.py` — Simplified dispatch_tool (~440→~50 lines)
- `src/mcp_handlers/response_formatter.py` — NEW: response mode filtering
- `src/mcp_handlers/core.py` — Response formatting extracted

---

## [2.6.1] - 2026-02-06

### Added — Name-Based Identity Resolution (PATH 2.5)

Every new HTTP session was creating a new agent UUID. 1650+ ghost agents existed because
session keys rotate per request. PATH 2.5 adds name-based identity claim: before creating
a new UUID, the server checks if the agent is claiming an existing name via label lookup
in PostgreSQL.

```
PATH 1: Redis cache by session_key       → found? use it
PATH 2: PostgreSQL session by session_key → found? use it
PATH 2.5: PostgreSQL agent by name claim  → found? bind + use it  ← NEW
PATH 3: Create new UUID                  → last resort
```

#### Identity Resolution
- **`resolve_by_name_claim()`** — New function in `identity_v2.py`. Looks up agent by
  label in PG, optionally verifies trajectory signature (anti-impersonation, rejects if
  lineage_similarity < 0.6), binds session in Redis + PG.
- **`resolve_session_identity()`** — New parameters `agent_name` and `trajectory_signature`.
  PATH 2.5 inserted before PATH 3.
- **`handle_identity_adapter()`** — Name claim runs before STEP 1 session resolution,
  preventing dispatch-created ephemerals from polluting the cache.
- **`handle_onboard_v2()`** — When `name` is provided and `not force_new`, tries
  `resolve_by_name_claim()` first before session-based lookup.
- **Dispatch (`__init__.py`)** — Extracts `agent_name` (check-in) or `name` (identity/onboard)
  and passes to `resolve_session_identity()`.
- **Schema (`tool_schemas.py`)** — Added `agent_name` parameter to `process_agent_update`.

#### Observe Tool Fix
- **Schema** — Added proper `inputSchema` for consolidated `observe` tool (was empty
  `properties: {}`, causing Pydantic typed wrapper to drop all parameters).
- **`target_agent_id`** — Renamed from `agent_id` to avoid clash with session-bound caller
  identity. Supports both UUID and label (e.g. `target_agent_id="Lumen"`).
- **Label resolution** — `handle_observe_agent()` resolves labels to UUIDs via
  `_find_agent_by_label()`, bypasses `require_agent_id`'s session-override behavior.

### Fixed
- Ghost agent proliferation: named agents reconnect instead of forking
- `observe_agent` alias dropping all parameters except `action`
- Session mismatch error when observing other agents
- `agent_name` parameter not extracted for identity/onboard tools in dispatch

### Database
- Added partial index: `idx_agents_label ON core.agents(label) WHERE label IS NOT NULL`
- Cleaned up test ghost agents (Tessera_* suffixed entries)

### Tests
- 1,907 tests passing, 0 failures, 41% coverage (at time of release; see v2.6.2 for latest)

### Files Changed
- `src/mcp_handlers/identity_v2.py` — +162 lines (resolve_by_name_claim + PATH 2.5 wiring)
- `src/mcp_handlers/__init__.py` — +19 lines (agent_name extraction in dispatch)
- `src/mcp_handlers/observability.py` — +57 lines (target_agent_id + label resolution)
- `src/tool_schemas.py` — +70 lines (observe schema + agent_name parameter)

---

## [2.6.0] - 2026-02-05

### Major Cleanup & Consolidation

This release removes ~4,200 lines of dead code, migrates dialectic sessions fully to PostgreSQL,
and expands test coverage from ~25% to 40% (1,798 tests passing).

### Removed - Dead Code (~4,200 lines)
- `identity.py` (v1) — Replaced by `identity_v2.py`
- `oauth_identity.py` — Never imported
- `governance_db.py` — Old SQLite backend, replaced by `postgres_backend.py`
- `knowledge_db_postgres.py` — Old PG backend, replaced by AGE graph
- `agent_id_manager.py`, `api_key_manager.py` — Unused
- `ai_behavior_analysis.py`, `ai_knowledge_search.py`, `ai_synthesis.py` — Replaced by `call_model`
- `mcp_server_compat.py`, `monitoring/`, `dual_log/INTEGRATION.py` — Unused

### Changed - Handler Refactoring
- **Tool surface reduced**: 49 → 29 registered tools (admin/internal tools hidden)
- **Dialectic backend**: Fully migrated from SQLite to PostgreSQL
- **Reviewer selection**: Simplified to random selection (user-facilitated model)
- **Consolidated handlers**: New `export()` and `observe()` unified tools
- **Deprecation**: `direct_resume_if_safe` → `quick_resume`/`self_recovery`
- **Tool schemas**: Added `client_session_id` parameter support

### Added - Dashboard & Infrastructure
- `dashboard/styles.css` — Extracted CSS for dashboard
- `scripts/migrate_dialectic_to_postgres.py` — Migration script (72 sessions migrated)
- `skills/unitares-governance/SKILL.md` — Agent onboarding guide
- System audit documentation

### Tests
- **43 new test files** covering pure logic, validators, helpers, integrations
- **1,798 tests passing**, 40% coverage (up from 458 tests, 25%)
- Cleanup fixture for `test_kwargs_unwrapping` to prevent ghost agent proliferation

### Fixed
- Dashboard `styles.css` now served (added to static file allowlist)
- Dialectic session listing uses PostgreSQL instead of stale SQLite
- Lumen governance check-ins restored (ngrok basic auth support in bridge)
- `_resolve_dialectic_backend()` now recognizes `postgres` as valid value

---

## [2.5.9] - 2026-02-05

### Added - Agent Circuit Breaker Enforcement

The agent "pause" status now actually blocks operations. Previously, setting `meta.status = "paused"` was purely cosmetic - agents could continue calling tools. Now it's enforced.

#### Enforcement Points
- **`process_agent_update`** — Paused agents cannot submit work updates
- **`store_knowledge_graph`** — Paused agents cannot store discoveries
- **`leave_note`** — Paused agents cannot leave notes

#### New Helper Function
- **`check_agent_can_operate(agent_uuid)`** — Reusable enforcement function
  - Returns `None` if agent can operate
  - Returns error `TextContent` if blocked (paused/archived)
  - Includes recovery guidance in error response

#### Recovery Path
- Paused agents receive clear error with recovery instructions
- Error includes: `self_recovery(action='resume')` or wait for auto-dialectic
- Error code: `AGENT_PAUSED` or `AGENT_ARCHIVED`

### Tests
- **9 new tests** for circuit breaker enforcement (`tests/test_circuit_breaker_enforcement.py`)
- Tests verify enforcement in handlers via source inspection
- Tests verify `check_agent_can_operate` blocks correctly

### Files Changed
- `src/mcp_handlers/core.py` — Added enforcement to `handle_process_agent_update`
- `src/mcp_handlers/knowledge_graph.py` — Added enforcement to `handle_store_knowledge_graph`, `handle_leave_note`
- `src/mcp_handlers/utils.py` — Added `check_agent_can_operate()` helper
- `tests/test_circuit_breaker_enforcement.py` — New test file

---

## [2.5.8] - 2026-02-05

### Added - Production-Grade Redis Resilience

#### Circuit Breaker Pattern
- **Fast failure when Redis is down** — Stops hammering Redis after 5 consecutive failures
- **Auto-recovery testing** — Transitions to HALF_OPEN after 30s to test if Redis recovered
- **State machine** — CLOSED → OPEN → HALF_OPEN → CLOSED lifecycle

#### Connection Pooling
- **Efficient connection management** — Default pool size of 10 connections
- **Configurable via env** — `REDIS_POOL_SIZE` environment variable

#### Retry with Exponential Backoff
- **Transient failure handling** — 3 retry attempts with exponential backoff
- **Configurable delays** — Base delay 0.1s, max delay 2.0s
- **Connection error detection** — Auto-reconnect on connection failures

#### Periodic Health Check
- **Reduced overhead** — Background health check every 30s instead of ping-per-call
- **Proactive failure detection** — Detects Redis failures before operations fail

#### Fallback Metrics
- **Comprehensive visibility** — Tracks operations, retries, fallbacks, connections
- **`get_redis_metrics()`** — Export metrics for monitoring dashboards
- **Success rate tracking** — Know when system is degraded

#### Redis Sentinel Support (HA)
- **High availability deployments** — Connect via Sentinel for automatic failover
- **`REDIS_SENTINEL_HOSTS`** — Comma-separated sentinel hosts
- **`REDIS_SENTINEL_MASTER`** — Master name for Sentinel discovery

### New Classes
- `CircuitBreaker` — Reusable circuit breaker pattern
- `RedisConfig` — Configuration dataclass with env var support
- `RedisMetrics` — Metrics collection and export
- `ResilientRedisClient` — Main client with all resilience features

### New Functions
- `get_redis_metrics()` — Get comprehensive health status
- `get_circuit_breaker()` — Access circuit breaker for monitoring
- `with_redis_fallback()` — Decorator for operations with fallback

### Environment Variables
```
REDIS_URL                      # Connection URL (default: redis://localhost:6379/0)
REDIS_ENABLED                  # Enable/disable Redis (default: 1)
REDIS_POOL_SIZE                # Connection pool size (default: 10)
REDIS_RETRY_ATTEMPTS           # Max retry attempts (default: 3)
REDIS_CIRCUIT_BREAKER_THRESHOLD # Failures before circuit opens (default: 5)
REDIS_CIRCUIT_BREAKER_TIMEOUT  # Seconds before retry after open (default: 30)
REDIS_SENTINEL_HOSTS           # Sentinel hosts (e.g., "host1:26379,host2:26379")
REDIS_SENTINEL_MASTER          # Sentinel master name (default: "mymaster")
```

### Tests
- **27 new tests** for Redis resilience (`tests/test_redis_resilience.py`)
- **449 tests passing** (up from 416)
- **31% coverage** maintained

### Files Changed
- `src/cache/redis_client.py` — Complete rewrite with resilience features (670 lines)
- `src/cache/__init__.py` — Updated exports for new classes/functions
- `tests/test_redis_resilience.py` — New comprehensive test suite

---

## [2.5.7] - 2026-02-05

### Changed - Identity Simplification & Code Organization

#### Three-Tier Identity Model
- **Simplified from four-tier to three-tier**:
  - `UUID` — Immutable internal identifier (primary key)
  - `agent_id` — Model+date format (e.g., `Claude_Opus_20251227`) for tracking
  - `display_name` — User-chosen name (merged with former `label` tier)
- `label` kept as backward-compat alias pointing to `display_name`
- Updated docstrings to document v2.5.3 three-tier model

#### Identity Module Refactoring
- **New `identity_shared.py`** — Shared utilities extracted from identity.py:
  - Session cache (`_session_identities`, `_uuid_prefix_index`)
  - Session key functions (`_get_session_key`, `make_client_session_id`)
  - Identity lookup (`get_bound_agent_id`, `is_session_bound`)
  - Permissions (`require_write_permission`)
  - Lineage utilities (`_get_lineage`, `_get_lineage_depth`)
- **Slimmed `identity.py`** — Now imports shared utilities, contains only async DB functions
- **Cleaner imports** — All modules now import from `identity_shared.py` for shared state

### Files Changed
- `src/mcp_handlers/identity_shared.py` — New shared module (280 lines)
- `src/mcp_handlers/identity_v2.py` — Updated to three-tier, uses identity_shared
- `src/mcp_handlers/identity.py` — Slimmed down, imports from identity_shared
- `src/mcp_handlers/__init__.py` — Updated imports
- `src/mcp_handlers/admin.py`, `lifecycle.py`, `knowledge_graph.py`, `oauth_identity.py` — Updated imports

### Tests
- **416 tests passing** (all existing tests still pass)
- **31% coverage** maintained

---

## [2.5.6] - 2026-02-05

### Added - UX Friction Fixes & Consolidated Tools

#### UX Friction Fixes (9 of 12 implemented)
- **Error code auto-inference** — `error_response()` now auto-infers error codes from message patterns (DATABASE_ERROR, TIMEOUT, NOT_FOUND, etc.)
- **Tool alias action injection** — Deprecated tool names automatically inject the correct `action` parameter when routing to consolidated tools
- **Parameter coercion reporting** — `_param_coercions` field shows what type conversions were applied
- **Lite response mode** — `lite_response=True` reduces output verbosity by excluding agent_signature
- **Error message sanitization** — Stack traces and internal paths stripped from error messages

#### Consolidated Tools
- **`config` tool** — Unified get/set thresholds (replaces `get_thresholds`, `set_thresholds`)
- **38+ tool aliases** — All legacy tool names map to consolidated tools with action injection
- **Better error guidance** — Unknown actions return `valid_actions` list with examples

#### LLM Delegation
- **`llm_delegation.py`** — Delegate tasks to smaller local/remote models
- **Ollama support** — Local model inference for knowledge synthesis
- **OpenAI fallback** — Remote model support when local unavailable

#### Dashboard Improvements
- **Modular components** — `components.js` for reusable UI elements
- **Shared utilities** — `utils.js` for common functions
- **Better structure** — Dashboard code reorganized for maintainability

#### Migration Cleanup
- **13 migration scripts archived** — Moved to `scripts/archive/migrations_completed_202602/`
- **Telemetry data ignored** — `data/telemetry/*.jsonl` added to `.gitignore`

### Changed
- Test suite expanded to **358 tests** (from 310+)
- Coverage at **30%** overall (core modules higher)
- Documentation updated with port configuration guides
- LICENSE updated with correct repository URL

### Fixed
- **Tool 'config' not found** — Added missing consolidated config tool
- **Alias injection not working** — Added `inject_action` field to `ToolAlias` dataclass
- **Test assertions** — Fixed test messages to match actual error patterns

### Files Changed
- `src/mcp_handlers/consolidated.py` — Added `config` tool
- `src/mcp_handlers/tool_stability.py` — Added `inject_action` to ToolAlias
- `src/mcp_handlers/utils.py` — Added `_infer_error_code_and_category()`, `_sanitize_error_message()`
- `src/mcp_handlers/validators.py` — Added coercion tracking
- `tests/test_ux_fixes.py` — 48 new tests for UX fixes
- `docs/TOOL_AUDIT_2026-02-04.md` — Tool audit documentation

---

## [2.5.5] - 2026-02-04

### Added - Trajectory Identity & Test Coverage

#### Trajectory Identity Framework
- **Genesis signature (Σ₀)** stored at first onboard, immutable thereafter
- **Lineage comparison** on each update - similarity to genesis tracked
- **Anomaly detection** when similarity drops below 0.6
- New functions: `store_genesis_signature()`, `update_current_signature()`, `verify_trajectory_identity()`, `get_trajectory_status()`

#### Model-Based agent_id Fix
- `agent_id` now properly uses model type when provided
- Format: `{Model}_{Version}_{Date}` (e.g., `Claude_Opus_4_5_20260204`)
- Fixed bug where `handle_onboard_v2` was ignoring properly generated `agent_id`

#### Test Coverage Expansion
- **93+ tests passing** (up from 25)
- `governance_monitor.py`: 79% coverage (63 tests)
- `trajectory_identity.py`: 88% coverage (19 tests)
- `identity_v2.py`: 11% coverage (11 tests)

#### New Test Classes
- `TestGainModulation` - HCK v3.0 PI gain modulation
- `TestEthicalDrift` - ‖Δη‖² computation
- `TestStatePersistence` - Save/load state
- `TestVoidFrequency` - Void frequency calculation
- `TestLambda1Update` - PI controller bounds
- `TestSimulateUpdate` - Dry-run without mutation
- `TestTrajectorySignature` - Dataclass serialization
- `TestGenesisStorage` - Immutable genesis
- `TestLineageComparison` - Similarity detection
- `TestVerifyIdentity` - Two-tier verification

### Changed
- Documentation updated to reflect actual system state
- "Ethical drift" section now correctly describes implemented functionality
- Roadmap updated with completed items

### Files Changed
- `src/mcp_handlers/identity_v2.py` - Fixed agent_id bug at lines 1446-1460
- `src/anima_mcp/unitares_bridge.py` - Wired trajectory signature to UNITARES
- `tests/test_governance_monitor_core.py` - 63 new tests
- `tests/test_trajectory_integration.py` - 19 new tests
- `tests/test_identity_agent_id.py` - 11 new tests
- `README.md` - Updated to reflect current state

---

## [2.5.4] - 2025-12-27

### Changed - Meaningful Identity in Knowledge Graph

#### Agent-Centric Identity (v2.5.4)
Agents find meaningful names more useful than UUID strings. This update shifts KG attribution from technical UUIDs to human-and-agent-readable identifiers.

#### Identity in Knowledge Graph
- **Before:** KG stored UUID (e.g., `a1b2c3d4-...`) - meaningless to agents and humans
- **After:** KG stores `agent_id` (e.g., `Claude_Opus_4_20251227`) - meaningful to both

#### Implementation
- `require_registered_agent()` now returns `agent_id` (model+date) for KG storage
- UUID kept internal via `_agent_uuid` for session binding only
- `_resolve_agent_display()` helper resolves agent_id to display info without exposing UUID
- Display names included in KG query responses for human readability

#### Four-Tier Identity Model (Refined)
1. **UUID** - Immutable technical identifier (internal only, never in KG)
2. **agent_id** - Model+date format (e.g., `Claude_Opus_4_20251227`) - stored in KG
3. **display_name** - User-chosen name ("birth certificate")
4. **label** - Nickname (can change)

### Files Changed
- `src/mcp_handlers/utils.py` - `require_registered_agent()` returns agent_id instead of UUID
- `src/mcp_handlers/knowledge_graph.py` - Added `_resolve_agent_display()` helper

---

## [2.5.1] - 2025-12-26

### Added - Three-Tier Identity Model

#### Identity Architecture
- **UUID** (immutable) - Technical identifier, never changes
- **agent_id** (structured) - Auto-generated on creation, stable (format: `{interface}_{date}`)
- **display_name** (nickname) - User-chosen via `identity(name=...)`, can change

#### New Fields
- `structured_id` field in `AgentMetadata` class
- `generate_structured_id()` function in `naming_helpers.py`

#### Response Updates
- `onboard()` and `identity()` now return all three tiers
- `compute_agent_signature()` includes `agent_id` in response
- Legacy fields (`agent_uuid`, `label`) preserved for compatibility

#### Migration Support
- Pre-v2.5.0 agents get `structured_id` generated on first `identity(name=...)` call

### Fixed - Honest Initialization for New Agents

#### Problem
New agents showed `coherence=1.0, risk=0.0` before their first check-in, then values "dropped" to ~0.55 after first `process_agent_update()`. This felt jarring - like something broke.

#### Solution
- Return `null` for computed metrics (`coherence`, `risk_score`) until first governance cycle
- Show `status: "uninitialized"` with clear messaging
- Display `⚪ pending (first check-in required)` instead of fake values

#### Before/After
**Before (misleading):**
```
coherence: 1.0  ← fake placeholder
risk: 0.0       ← fake placeholder
```

**After (honest):**
```
status: ⚪ uninitialized
coherence: null (pending)
risk: null (pending)
next_action: 📝 Call process_agent_update() to start governance tracking
```

### Files Changed
- `src/mcp_server_std.py` - Added `structured_id` field to AgentMetadata
- `src/mcp_handlers/naming_helpers.py` - Added `generate_structured_id()` function
- `src/mcp_handlers/identity.py` - Three-tier model in responses
- `src/mcp_handlers/identity_v2.py` - Three-tier model + migration
- `src/mcp_handlers/utils.py` - Updated `compute_agent_signature()`
- `src/governance_monitor.py` - Return `null` for metrics when `update_count == 0`
- `src/mcp_handlers/core.py` - Show "uninitialized" status with helpful messaging

---

## [2.5.0] - 2025-12-26

### Added - HCK/CIRS Stability Monitoring

#### HCK v3.0 - Reflexive Control
- **Update coherence ρ(t)** - Measures directional alignment between E and I updates
  - ρ ≈ 1: Coherent updates (E and I moving together)
  - ρ ≈ 0: Misaligned or unstable
  - ρ < 0: Adversarial movement (E and I diverging)
- **Continuity Energy (CE)** - Tracks state change rate ("work required to maintain consistency")
- **PI Gain Modulation** - When ρ is low, controller gains are reduced to prevent instability

#### CIRS v0.1 - Oscillation Detection & Resonance Damping
- **New `src/cirs.py`** - Complete CIRS implementation
- **OscillationDetector** - Tracks threshold crossings via EMA of sign transitions
  - Oscillation Index (OI) = EMA(sign(Δcoherence)) + EMA(sign(Δrisk))
  - Flip counting for decision/route changes
- **ResonanceDamper** - Adjusts thresholds when resonance detected
- **Response tiers:**
  - `proceed` - Normal operation
  - `soft_dampen` - Resonance detected but not critical
  - `hard_block` - Critical safety pause

#### New State Fields
- `rho_history`, `CE_history`, `current_rho` - HCK tracking
- `oi_history`, `resonance_events`, `damping_applied_count` - CIRS tracking

### Fixed

#### Session Identity Bug
- **Issue:** `onboard` created agent X, but `process_agent_update(client_session_id=...)` used different agent Y
- **Root cause:** `onboard`/`identity` only registered binding in memory, not Redis; `identity_v2` couldn't find it
- **Fix:** Added Redis caching in `identity.py` after stable session binding registration
- **Location:** `mcp_handlers/identity.py:1355-1365` and `mcp_handlers/identity.py:1559-1566`

### Changed

#### Governance Output
- `process_agent_update` now includes `hck` and `cirs` sections in response
- `get_metrics` includes HCK/CIRS metrics
- New response tier field indicates `proceed`/`soft_dampen`/`hard_block`

### Documentation
- Updated `.agent-guides/DEVELOPER_AGENTS.md` with HCK/CIRS architecture
- Updated `.agent-guides/FUTURE_CLAUDE_CODE_AGENTS.md` with v2.5.0 reference
- Updated `.agent-guides/SSE_SERVER_INFO.md` with correct script reference

---

## [2.4.0] - 2025-12-25

### Added - Simplified Identity System (identity_v2)

#### 3-Path Architecture
- **New `identity_v2.py`** - Replaces complex 15+ code path identity system
- **Three resolution paths only:**
  1. Redis cache (fast path, < 1ms)
  2. PostgreSQL session lookup
  3. Create new agent
- **Cleaner separation of concerns:**
  - `resolve_session_identity()` → "Who am I?" (session → UUID)
  - `get_agent_metadata()` → "Who is agent X?" (lookup by UUID/label)

#### Database Enhancements
- **Added `label` column** to `core.agents` table
- **New PostgresBackend methods:**
  - `get_agent()` - Full agent record retrieval
  - `get_agent_label()` - Fast label lookup
  - `find_agent_by_label()` - Label collision detection
  - Extended `update_agent_fields()` with `label` parameter

### Changed

#### Identity Tool
- **`identity()` tool** now uses simplified v2 handler
- **Label is just metadata** - Not an identity mechanism (reduces confusion)
- **Consistent UUID** - Same session always returns same UUID (fixes Bug #3)

#### UX Improvements
- **Auto-semantic search** - Multi-word queries auto-use semantic search when available
- **Pagination for discoveries** - `get_discovery_details` now supports `offset`/`length`
- **Search hints** - Helpful suggestions when substring search returns no results

### Fixed
- **Bug #2:** `get_agent_metadata` UnboundLocalError (`attention_score` → `risk_score`)
- **Bug #3:** Identity binding inconsistencies causing UUID confusion

### Deprecated
- **Old identity.py handler** - `@mcp_tool` decorator commented out, kept for reference
- **`hello()`/`status()` pattern** - Use `identity()` instead (aliases still work)

---

## [2.3.0] - 2025-12-01

### Added - Complete Decorator Migration 🎯

#### 100% Migration to Decorator Pattern
- **All 43 tools migrated** to `@mcp_tool` decorator pattern
- **Automatic timeout protection** on all tools
- **Self-documenting code** - timeout values attached to functions
- **Enhanced `list_tools`** - Now includes timeout and category metadata

#### Final Migrations
- `process_agent_update` (60s timeout) - Most complex handler
- `simulate_update` (30s timeout)
- `health_check` (10s timeout)
- `get_workspace_health` (30s timeout)
- `delete_agent` (15s timeout)

### Changed

#### Timeout Protection
- **Removed double timeout wrapping** - `dispatch_tool()` no longer wraps with 30s timeout
- **Decorator timeouts now effective** - Each tool uses its configured timeout
- **Critical improvement:** `process_agent_update` now uses 60s timeout (was 30s)

#### UX Improvements
- **Reframed pause messages** - More supportive and collaborative language
  - "High complexity detected" → "Complexity is building - let's pause and regroup"
  - "safety pause required" → "safety pause suggested"
  - Added: "This is a helpful pause, not a judgment"

#### Tool Metadata
- **Enhanced `list_tools` output** - Now includes timeout values and categories
- **Better tool discovery** - Agents can see timeout requirements when discovering tools

### Technical Improvements
- **Consistent pattern** - Single decorator pattern across all 43 tools
- **Less boilerplate** - Auto-registration reduces manual dict entries
- **Better error handling** - Standardized timeout error responses

---

## [2.2.0] - 2025-11-28

### Added - Knowledge Graph System 🚀

#### Fast, Indexed Knowledge Storage
- **Knowledge Graph Engine** (`src/knowledge_graph.py`) - Complete in-memory graph implementation
  - O(1) inserts with automatic index updates
  - O(indexes) queries (not O(n)) - scales logarithmically
  - Tag-based similarity search (no brute force scanning)
  - Async background persistence (non-blocking)
  - Claude Desktop compatible (no blocking I/O)

#### New MCP Tools (6 tools)
- `store_knowledge_graph` - Store discoveries (35,000x faster than file-based)
- `search_knowledge_graph` - Search by tags, type, agent, severity (indexed queries)
- `get_knowledge_graph` - Get agent's knowledge (fast index lookup)
- `list_knowledge_graph` - Get graph statistics (full transparency)
- `update_discovery_status_graph` - Update discovery status (open/resolved/archived)
- `find_similar_discoveries_graph` - Find similar by tag overlap (3,500x faster)

#### Migration Tool
- `scripts/migrate_to_knowledge_graph.py` - One-time migration from file-based to graph
- Preserves all relationships and metadata
- Converts existing 252 discoveries automatically

### Changed

#### Performance Improvements
- **Knowledge operations**: 35,000x faster (`store_knowledge`: 350ms → 0.01ms)
- **Similarity search**: 3,500x faster (`find_similar`: 350ms → 0.1ms)
- **Query performance**: O(indexes) instead of O(n) file scans
- **Claude Desktop compatibility**: All operations non-blocking

#### File Organization
- **Root directory cleanup** - Moved 7 markdown files to organized locations:
  - `ARCHITECTURE.md` → `docs/architecture/`
  - `ONBOARDING.md` → `docs/guides/`
  - `USAGE_GUIDE.md` → `docs/guides/`
  - `SYSTEM_SUMMARY.md` → `docs/reference/`
  - `METRICS_REPORTING.md` → `docs/guides/`
  - `ARCHIVAL_SUMMARY_20251128.md` → `docs/archive/`
  - `HARD_REMOVAL_SUMMARY_20251128.md` → `docs/archive/`
- **Root directory**: Now contains only `README.md`, `CHANGELOG.md`, and `requirements-mcp.txt`

### Fixed

#### Knowledge Layer Issues
- **Claude Desktop freezing** - Fixed blocking I/O with async graph operations
- **Context compression** - Indexed queries prevent large response issues
- **Performance bottlenecks** - Graph-based approach eliminates O(n×m) scans

### Documentation

#### Created
- `docs/proposals/KNOWLEDGE_GRAPH_DESIGN.md` - Complete design proposal
- `docs/guides/KNOWLEDGE_GRAPH_USAGE.md` - Usage guide with examples
- `docs/guides/KNOWLEDGE_GRAPH_INTEGRATION_COMPLETE.md` - Integration summary
- `docs/analysis/KNOWLEDGE_GRAPH_IMPLEMENTATION_SUMMARY.md` - Implementation details
- `docs/analysis/KNOWLEDGE_GRAPH_USAGE_VERIFICATION.md` - Verification results
- `docs/proposals/KNOWLEDGE_GRAPH_TRANSPARENCY.md` - Transparency design
- `docs/analysis/KNOWLEDGE_GRAPH_FINAL_APPROACH.md` - Final approach documentation
- `docs/analysis/MODEL_CLIENT_STRATEGY.md` - Model/client strategy analysis
- `docs/ROOT_FILE_ORGANIZATION.md` - Root file organization guide

#### Updated
- `docs/DOC_MAP.md` - Updated paths for moved files
- `docs/README.md` - Updated root file references
- `docs/DOCUMENTATION_GUIDELINES.md` - Updated onboarding path
- `src/tool_usage_tracker.py` - Updated archive summary path
- `scripts/check_small_markdowns.py` - Updated file list
- `scripts/validate_project_docs.py` - Updated validation paths

### Technical Details

#### Architecture
- **Graph structure**: Nodes (discoveries) with 5 indexes (agent, tag, type, tag, type, severity, status)
- **Persistence**: Single JSON file (`data/knowledge_graph.json`) with async background saves
- **Debouncing**: 100ms delay for rapid writes (efficient batching)
- **Error handling**: Graceful degradation (starts empty if load fails)

#### Integration
- **Handler registry**: All 6 tools registered in `src/mcp_handlers/__init__.py`
- **MCP server**: All tools defined with complete schemas in `src/mcp_server_std.py`
- **Tool list**: Included in `list_tools()` for runtime introspection

### Performance Metrics

- **Store operation**: ~0.01ms (vs 350ms file-based) - **35,000x faster**
- **Similarity search**: ~0.1ms (vs 350ms file-based) - **3,500x faster**
- **Query performance**: O(indexes) not O(n) - **scales logarithmically**
- **Memory usage**: ~1MB for 252 discoveries, scales to 10,000+ efficiently

---

## [2.1.0] - 2025-11-25

### Added - Auto-Healing Infrastructure 🛡️

#### Enhanced State Locking System
- **Automatic stale lock detection** - `is_process_alive()` checks for dead processes
- **Smart lock cleanup** - `_check_and_clean_stale_lock()` removes locks from crashed processes
- **Exponential backoff retry** - 3 attempts with 0.2s * 2^attempt wait times
- **Process health checking** - Validates PIDs before lock cleanup
- **Self-recovering** - No manual intervention needed for lock contention

#### Loop Detection & Prevention
- **Activity tracking** - New AgentMetadata fields:
  - `recent_update_timestamps: list[str]` - Track update timing
  - `recent_decisions: list[str]` - Track decision patterns
  - `loop_detected_at: str` - Timestamp of loop detection
  - `loop_cooldown_until: str` - Block updates until this time
- **Pattern detection** - Identifies infinite update loops
- **Automatic cooldown** - Enforces waiting period when loops detected

#### Agent Hierarchy & Spawning
- **Parent/child tracking** - New AgentMetadata fields:
  - `parent_agent_id: str` - Which agent spawned this one
  - `spawn_reason: str` - Why agent was spawned (e.g., "new_domain")
  - `api_key: str` - Unique authentication key per agent
- **Multi-agent support** - Track lineage and dependencies
- **Security** - API keys prevent unauthorized agent impersonation

#### Modular Handler Architecture
- **Handler registry pattern** - Organized 29 handlers into `src/mcp_handlers/`
- **Category organization**:
  - `core.py` - Core governance operations
  - `config.py` - Configuration management
  - `observability.py` - Monitoring and metrics
  - `lifecycle.py` - Agent lifecycle management
  - `export.py` - Data export functionality
  - `knowledge.py` - Knowledge layer operations
  - `admin.py` - Administrative tools
  - `dialectic.py` - Dialectic protocol
  - `utils.py` - Common utilities
- **Standardized error handling** - `require_agent_id()`, `success_response()`, `error_response()`

#### New Tools & Scripts
- `~/scripts/fix_cursor_freeze.sh` - One-command recovery tool for Cursor/IDE freezes
- `~/scripts/test_enhanced_locking.py` - Comprehensive lock system test suite (4 tests)
- `~/scripts/test_mcp_json_rpc.py` - MCP protocol verification tool
- `~/scripts/diagnose_cursor_mcp.sh` - Complete system diagnostic script

### Changed

#### Increased Capacity
- **MAX_KEEP_PROCESSES** - Increased from 36 to 42
- **Better concurrency** - Support for Cursor + Claude Desktop + other MCP clients simultaneously

#### Enhanced Reliability
- **Lock acquisition** - Now includes automatic stale lock cleanup before retry
- **Error messages** - More detailed, actionable error messages with recovery suggestions
- **Async support** - Added optional `aiofiles` support with graceful fallback

### Fixed

#### Critical Fixes
- **Cursor freeze issue** - Auto-healing locks prevent lock contention from duplicate servers
- **Dialectic protocol bug** - Fixed `'str' object is not a mapping` error (AgentMetadata → dict conversion)
- **JSON-RPC protocol** - All debug output redirected to stderr (was breaking Claude Desktop)
- **Lock cleanup** - Automatic cleanup on MCP server startup (5-minute staleness threshold)

#### Previous Session Fixes (Pre-v2.1)
- **Process cleanup** - Zombie process detection and cleanup on startup
- **Agent archival** - Auto-archive test agents after 7 days
- **Metadata locking** - Enhanced file-based locking during reads/writes

### Documentation

#### Updated
- **README.md** - Added v2.1 feature section
- **QUICK_REFERENCE.md** - Added troubleshooting section for Cursor freezes and new tools
- **CHANGELOG.md** - Created comprehensive changelog (this file)

#### Created
- `~/scripts/cursor_implementations_summary.md` - Complete v2.1 feature documentation
- `~/scripts/session_final_status.md` - Session summary and status

#### Consolidated
- Archived 5 redundant session documentation files to `~/scripts/Archive/session_docs_20251125/`
- Kept 2 comprehensive docs: `cursor_implementations_summary.md` and `session_final_status.md`

### Testing

#### Test Results
- **Enhanced Locking Tests**: 4/4 passed ✅
  - Process health check ✅
  - Lock acquisition ✅
  - Stale lock cleanup ✅
  - Retry logic ✅
- **MCP JSON-RPC Tests**: 2/2 passed ✅
  - Governance MCP ✅
  - Date Context MCP ✅
- **System Health**: All checks pass ✅

### Git Statistics
- **Commit**: 921bde6
- **Files changed**: 157
- **Insertions**: +32,856 lines
- **Deletions**: -2,969 lines
- **Net change**: +29,887 lines

---

## [2.0.0] - 2025-11-24

### Added - Complete System Implementation

#### Elegant Handler Architecture
- Refactored MCP server from 1,700+ line elif chain to clean handler registry (~30 lines)
- 29 handlers organized by category
- Zero elif branches - maintainable and testable

#### All 5 Decision Points Implemented
1. **λ₁ → Sampling Parameters** - Linear transfer function
2. **Risk Estimator** - Multi-factor risk scoring
3. **Void Detection Threshold** - Adaptive threshold (mean + 2σ)
4. **PI Controller** - Concrete gains (K_p=0.5, K_i=0.05)
5. **Decision Logic** - Risk-based approve/revise/reject

#### UNITARES Framework
- Complete thermodynamic governance implementation
- E (Engagement), I (Integrity), S (Safety), V (Void) metrics
- Coherence tracking and health monitoring
- State persistence and history

#### CLI Tools
- `agent_self_log.py` - CLI logging with full state persistence
- `register_agent.py` - Simple agent registration
- `claude_code_bridge.py` - Bridge for Claude Code integration

### Changed
- Project structure reorganized for clarity
- Documentation consolidated and updated
- Tests expanded and verified

### Documentation
- Complete README with architecture overview
- ONBOARDING.md for new users
- Multiple guides in docs/ directory
- README_FOR_FUTURE_CLAUDES.md

---

## [1.0.0] - 2025-11-20

### Initial Release
- Core UNITARES framework
- Basic MCP server implementation
- Agent metadata tracking
- File-based state persistence
- Simple decision logic

---

## Upgrade Guide

### From v2.1 to v2.2

**New features automatically available:**
- Knowledge graph tools ready to use
- Migration tool available for existing data
- All operations non-blocking (Claude Desktop compatible)

**Optional - Migrate existing knowledge:**
```bash
python3 scripts/migrate_to_knowledge_graph.py
```

**New tools available:**
- `store_knowledge_graph` - Fast discovery storage
- `search_knowledge_graph` - Indexed knowledge queries
- `get_knowledge_graph` - Get agent knowledge
- `list_knowledge_graph` - Graph statistics
- `update_discovery_status_graph` - Update discovery status
- `find_similar_discoveries_graph` - Find similar discoveries

**No breaking changes.** Old file-based knowledge layer archived but preserved.

### From v2.0 to v2.1

**No breaking changes.** Simply pull the latest code:

```bash
git pull
```

**New features automatically enabled:**
- Auto-healing locks (no configuration needed)
- Loop detection (automatically tracks agents)
- Enhanced capacity (MAX_KEEP_PROCESSES already increased)

**Optional - Install new tools:**
```bash
chmod +x ~/scripts/fix_cursor_freeze.sh
chmod +x ~/scripts/diagnose_cursor_mcp.sh
```

**If experiencing Cursor freezes:**
```bash
~/scripts/fix_cursor_freeze.sh
```

**To test new locking system:**
```bash
python3 ~/scripts/test_enhanced_locking.py
```

---

## Known Issues

### v2.6.2
- `test_get_governance_metrics` flaky when run with full suite (test ordering issue)
- Knowledge graph doesn't close loops well — resolve or archive discoveries manually

### Workarounds
All known issues have fallback behavior and don't block functionality.

---

## Future Roadmap

### In Progress
- Outcome correlation — does high instability actually predict bad outcomes?
- Threshold tuning — domain-specific drift thresholds need real-world calibration

### Under Consideration
- WebSocket dashboard updates (replace polling)
- CIRS v1.0 — full multi-agent oscillation damping
- Semantic ethical drift detection (beyond parameter changes)
- Production hardening and horizontal scaling

---

**Maintained by:** UNITARES Development Team
**License:** See LICENSE file
**Repository:** governance-mcp-v1
