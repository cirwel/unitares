# Dialectic reviewer hosts from the inference registry (v0)

Status: Proposed, design-only. Merging this document does not by itself
authorize the build. The operator delegated the design ("proceed best for
federation future", 2026-10-04); three questions that remain the operator's
are in section 7.

Date: 2026-10-04

## 1. Problem

The orchestrated dialectic reviewer (`agents/dialectic_reviewer/`) reaches
models through its own lane, not through the inference registry
(`src/mcp_handlers/support/inference_registry.py`). The registry's own Codex
entry says so (`inference_registry.py:322-325`). Three consequences follow.

**One host, then the local model.** `obtain_reviewer_text`
(`reviewer.py:668-743`) reads one name from `UNITARES_DIALECTIC_REVIEWER_HOST`.
When that host fails, the reviewer falls straight to the local endpoint, and
`withhold_fallback_approval` (`reviewer.py:417-460`) turns any approval from
that fallback into an abstention. The local model may object but never
approve, and that rule is right: in the 2026-07-02 planted-flaw probe it
affirmed the flaw (`call_codex_reviewer` docstring). So every failure of the
one host ends without an approval.

The record, per dispatched session (`audit.events` `dialectic_reviewer_dispatched`
joined to the first antithesis and to `dialectic_reviewer_abstained`, keyed on
`payload->>'session_id'`; 114 sessions, none counted twice). Sessions are
classified by the host their provenance names, not by date, because the
periods overlap on 09-23 and 09-25:

| Host | Period | Host verdict | Fell back or abstained |
|---|---|---|---|
| codex | 2026-08-24 to 09-23 | 83 of 88 | 5 |
| external (`gemini-3.8-flash`) | 2026-09-23 to 09-25 | 3 of 8 | 5 (3 `InternalServerError`, 2 truncated) |
| antigravity | 2026-09-25 to 10-03 | 8 of 14 | 6 (4 fallbacks, 2 with no antithesis), every one ending `failed` |

The periods differ and the later samples are small, so the table shows a
direction, not rates. `failed` is not only a fallback outcome: five sessions
with a host verdict also ended `failed` (four codex, one external). What the
table does show is that every fallback or abstention in the antigravity
period ended `failed`, so the cost of a single host is paid in failed
dialectics, whichever host it is. The live host was switched back to `codex`
on 2026-10-04.

**A new provider means reviewer code.** Each backend is a branch in
`obtain_reviewer_text` with its own client. Four of the five duplicate a path
the registry already has (codex, claude, antigravity, the local endpoint), and
the fifth, the OpenAI-compatible `external` backend, exists only in the
reviewer. Adding the Z.ai GLM free tier, or a future provider such as Meta's,
would be a sixth branch.

**Independence is not checked.** `consult(effort='thorough')` never routes a
caller to its own model family (`consultation.py:112-121`). The dialectic
reviewer has no such rule, and nothing tells it the paused agent's family: the
dispatcher builds the reviewer's environment from the thesis and pause
evidence (`orchestrator_dispatch.py:111-147`) and an allowlist of reviewer
settings (`:148-192`), and no code path resolves the paused agent's
`model_type`. Today a codex agent is reviewed by the `codex` host.

## 2. Proposal

### 2.1 An ordered list the system may only shorten

`UNITARES_DIALECTIC_REVIEWER_HOSTS` takes an ordered, comma-separated list of
at most three host ids, for example `codex,antigravity`. The reviewer tries
them in the operator's order.

The system may only remove hosts from that order, and only for the reasons
this design names: no judgment (below), a cooldown (2.5), or the paused
agent's family (2.3). It never adds a host, reorders the list, or chooses by
cost, latency, quality or past verdicts. That is the line between this and a
router, and a test asserts the order is never permuted.

A host's attempt ends the list when its reply parses to a formed judgment:
`parse_reviewer_verdict` returns `judgment_formed` with an explicit `agrees`.
The check runs where the reply is parsed, not on raw text, because today a
codex reply counts as text as soon as it holds any JSON object
(`extract_last_json_object`). A reply that parses as an objection is an
objection even when it was truncated, so inducing a truncation cannot shop for
the next host. An error, a timeout, a nonzero exit or a reply with no formed
judgment moves to the next host. After the list, the local endpoint runs as
the floor.

`UNITARES_DIALECTIC_REVIEWER_HOST` stays as a one-item list, under the
expiring-alias rule of `local-inference-one-endpoint-v0.md` section 2.1.1. The
new name joins the dispatcher's forwarding allowlist
(`orchestrator_dispatch.py:148-192`) beside the old one.

### 2.2 Three authorities, declared per host

Listing a host today would bundle three authorities: it may receive the
dialectic content, it may be called, and its verdict may approve. They are
declared separately, on the host's registry entry:

- `egress_class`: what data the host may receive. Listing a host for
  dialectic review requires a class that admits agent work content.
- `may_approve`: whether its verdict can release a paused agent. True on the
  built-in codex, claude and antigravity entries; false on the local endpoint,
  `ollama:local` and `hf:router`; false by default on declared hosts (2.4).
- `authorized_by`: the operator principal that declared or listed the host.
  One operator today; recorded so a later federation can tell whose trust
  released an agent.

**Approval requires an affirmative mark.** `withhold_fallback_approval` stops
keying on `fallback_from` and keys on `vouched`, a provenance field set to
`true` only on the code path where a listed host with `may_approve` formed the
judgment in this call. A verdict whose provenance lacks the field is withheld.
A key that defaults to approval would fail open on every path that forgets to
set it; this one fails closed.

**The floor cannot be listed.** Configuration load rejects a list that names
the local endpoint, by endpoint identity rather than by name: `ollama:local`,
or any host whose base URL resolves to `UNITARES_MODEL_BASE_URL` or its
fallback. Otherwise listing it would launder the 07-02 rule.

**With no list set, behavior is unchanged.** An install with no reviewer host
runs the local model as its reviewer today, with no `fallback_from`, and that
reviewer can approve. This design keeps that: the `vouched` rule applies when
a list is set. Whether a no-list install's local reviewer should keep approval
authority is outside this design; section 6 records it.

**Provenance is per call.** `obtain_reviewer_text` returns its provenance with
its text instead of writing the process-global `_LAST_REVIEWER_PROVENANCE`
(`reviewer.py:98`), so a later call that raises before recording cannot leave
an earlier call's `vouched` in place.

**The answering host is pinned for the session.** The host whose judgment
formed the first verdict answers every later call in the session: the
continuation after a disagreement (`reviewer.py:1103`) and the repair call
(`:1183`). If the pinned host fails on a later call, that call's result is
block-only; the list does not restart. Otherwise codex could object, time out
on the continuation, and antigravity could approve a thesis codex never
accepted.

### 2.3 Independence from the paused agent's family

**A closed vocabulary.** Families are `anthropic`, `openai`, `google`, `meta`,
`zhipu`, `other:<slug>` or `unknown`. A host entry with no family is rejected
at load. `consult`'s `_family_of` (`consultation.py:198`) and
`_THOROUGH_PEERS` (`:112-121`), which today map a caller string to peer host
ids, are restructured to read the same vocabulary, so the two lanes share one
definition.

**Family belongs to the model served, not the host.** Antigravity serves
Claude and GPT-OSS models as well as Gemini (`consultation.py:211-219`), and
an aggregator such as `hf:router` serves whatever the request names. A host
entry therefore declares its family per pinned model, and a host that can
serve several families must pin one. Where a response reports `model_used`,
its family is checked against the entry; where it does not (antigravity and
codex report none today), the entry's declared family is recorded as declared.

**The paused agent's family.** The dispatcher resolves it, a lookup that does
not exist today, from the most trusted source available: an adapter- or
substrate-recorded model first, the agent's self-declared `model_type`
(`core.identities.metadata->>'model_type'`) last, because an agent can
mislabel itself to steer its review. It forwards `DIALECTIC_PAUSED_AGENT_FAMILY`
and the source it used.

**The rule.** A listed host of the paused agent's family is skipped. An
approval whose served family turns out to match the paused family is
withheld. When either family is `unknown`, nothing is skipped and the verdict
records `independence: unverified`; whether such an approval counts is
question 7.3.

### 2.4 OpenAI-compatible providers are declared entries

The reviewer's `external` backend generalizes to hosts declared in one
operator file, `UNITARES_INFERENCE_HOSTS_FILE`. The schema is fixed in this
design so that later steps do not change its meaning:

| Field | Meaning |
|---|---|
| `id` | `external:<operator>/<name>`, namespaced per operator |
| `base_url`, `model` | OpenAI-compatible base and the pinned model |
| `api_key_env` | the name of the variable holding the key, never its value (`local-inference-one-endpoint-v0.md` 2.6) |
| `family` | from the vocabulary in 2.3 |
| `egress_class`, `may_approve`, `authorized_by` | as in 2.2; `may_approve` defaults to false |
| `timeout_s` | per-call budget |

Entries register in the registry, so `list_inference_hosts` shows them, with
`accepts_host_id_from: ["dialectic_reviewer"]`. They are reachable only from
the reviewer list. Exposing them to `call_model` or `consult` is a separate
decision, governed by those tools' contracts and by the one-endpoint design's
non-goals. The single-`external` settings (`UNITARES_DIALECTIC_EXTERNAL_*`)
become an alias for one entry.

### 2.5 Cooldowns

A host that fails on quota or authentication records a cooldown through
`host_availability.record_unavailable_async` (Redis key
`unitares:host_cooldown:…`, `host_availability.py:82`), and the reviewer loads
cooldowns at start with `load_from_redis()` (`:386`). A host cooling down is
skipped without a call, which saves the full timeout on a quota known to be
exhausted.

The key becomes (host id, credential): the CLI login for a host adapter, the
`api_key_env` name for a declared host. The reviewer and `consult` may hold
different credentials with different quotas, and one exhausted credential must
not hide a healthy one. The reviewer runs as an orchestrator child, so the
dispatcher forwards the Redis location as configuration. If Redis cannot be
read, the reviewer proceeds without cooldowns and records a warning.

### 2.6 What every verdict records

Each verdict's provenance carries:
- the effective list and a digest of the host configuration;
- every attempt, with its host, outcome and reason;
- the answering host's declared family and the `model_used` it reported;
- the paused agent's family and its source;
- `independence`, `vouched` and `authorized_by`.

These are recorded from step 1, before the rules that use them, because a
verdict written without them can never be re-attributed.

### 2.7 Time budget

`_reviewer_max_runtime_ms` (`orchestrator_dispatch.py:89`) already adds
`REVIEWER_RUNTIME_GRACE_S` (900 s, `:54`), which covers one model call. Each
further listed host adds its timeout: with today's defaults (420 s for codex,
claude and antigravity, 180 s for a declared host) a three-host list adds at
most 840 s. Pinning (2.2) means continuation rounds call one host and do not
repay the list. The antithesis still lands far inside `MAX_ANTITHESIS_WAIT`
(two hours).

## 3. Non-goals

- No router: the system only removes hosts from the operator's order (2.1).
  The ratified "one primary, one optional fallback" in
  `local-inference-one-endpoint-v0.md` governs the local lane and is
  unchanged; this design is about the reviewer's host list.
- No metered default. The list is empty unless the operator sets it.
- No change to `consult`'s or `call_model`'s contract. 2.3 changes only where
  `consult` reads family from.
- The reviewer keeps its own subprocess calls for codex, claude and
  antigravity. Routing them through `delegate_inference` would make the
  reviewer, itself an orchestrator child, spawn a second child. Sharing host
  records, families and cooldowns is the goal here; sharing the call path is
  later work.

## 4. Staging

Each step is its own pull request, and each preserves behavior for an install
that changes no setting.

1. **The list, with the trust rules.** The list and its cap, failover on no
   formed judgment, `vouched` with the fail-closed default, the floor rejected
   from the list, per-call provenance, pinning, the lifetime cap, and the
   provenance fields of 2.6 (families recorded only). Live setting after it
   deploys: `codex,antigravity`.
2. **Cooldowns** (2.5).
3. **Independence** (2.3): the vocabulary, the dispatcher's lookup, the skip
   and the withhold.
4. **Declared hosts** (2.4), with the schema as fixed above.

`agents/dialectic_reviewer/host_backends.py` and the approval path are
security-sensitive under `scripts/dev/review_policy.json`; steps 1 and 3 need
reviews from two model families.

## 5. Evidence required before each step ships

- Step 1: the first host fails and the second approves, and the approval
  stands; every host fails and the local floor's approval is withheld; a
  verdict whose provenance lacks `vouched` is withheld; a list naming the
  local endpoint, by name or by URL, is rejected at load; a continuation whose
  pinned host fails does not approve; the order is never permuted. Live: a
  canary through the list.
- Step 2: a cooled-down host is skipped without a call, and a Redis outage
  produces a warning, not a failure.
- Step 3: a codex-family paused agent with `codex,antigravity` listed is
  reviewed by antigravity, with the skip in provenance; an approval whose
  reported model matches the paused family is withheld.
- Step 4: a declared host answers a canary review end to end, and the key's
  value never appears in the spawn record.
- After step 1, the table in section 1 is re-read from provenance for every
  host, with each failure's recorded reason, before the order is changed.

## 6. Risks

- **Wider approval authority.** A second listed host can approve. That is the
  intent, bounded by `may_approve`, the floor exclusion and pinning, and the
  provenance names the host that approved.
- **The no-list local reviewer keeps approval authority.** That is today's
  behavior for an install with no host configured, kept so that such an
  install can still clear a paused agent. It is the same model the 07-02 probe
  showed cannot judge; changing it is a separate decision.
- **Quota spent on failover.** A failure that costs quota (a timeout after
  long reasoning) can be followed by a second host's call. Failover happens
  only without a formed judgment, and cooldowns skip a known-exhausted host.
- **Data leaving the machine.** Each listed host receives the thesis and the
  pause evidence. `egress_class` and the registry's privacy class are shown by
  `list_inference_hosts` and the doctor, so a host with unknown retention is
  visible before it is listed.

## 7. Open questions for the operator

1. **GLM's data retention.** Z.ai's documentation says nothing about retaining
   prompts, and the key in `ZAI_API_KEY` was made for toy prompts only. A
   dialectic prompt carries an agent's thesis and pause evidence. Should GLM
   be listed for dialectic review before its retention is known? Until
   answered, step 4 declares the entry with an `egress_class` that does not
   admit agent work content, so it cannot be listed.
2. **Approval from a later host (2.2).** Proposed: any listed host with
   `may_approve` may approve. The alternative keeps today's rule (only the
   first may) and accepts a failed dialectic whenever it is out. Recommended:
   any listed host.
3. **Unverified independence (2.3).** When the paused agent's family or the
   answering model's family is unknown, should its approval count? Counting
   it, recorded as `unverified`, keeps such agents clearable; withholding it
   means an agent with an unrecognized `model_type` can never be released by
   review. Recommended: count it, recorded, and revisit with data from the
   provenance.
