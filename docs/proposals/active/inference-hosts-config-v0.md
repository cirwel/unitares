# Inference hosts and jobs from one operator file (v0)

Status: Accepted 2026-10-15. The operator's decisions are in section 7, and he
authorized building from step 1 ("proceed all"). Each step still ships as its
own pull request with the evidence in section 5. It extends `dialectic-reviewer-hosts-v0.md` from the
dialectic reviewer to the other inference jobs, and it leaves the local lane
exactly as `local-inference-one-endpoint-v0.md` defines it. Section 7 records
the operator's decisions of 2026-10-15, including one that widens the reviewer
design.

Date: 2026-10-15

## 1. Problem

UNITARES asks other models for help in four places: `consult`, the hidden
`call_model` and `delegate_inference`, the dialectic reviewer, and the pull
request review gate. Each keeps its own idea of which hosts exist, which family
each belongs to, and which to try first.

- **Hosts are code.** `consult` takes five host ids and `delegate_inference`
  three, as Pydantic `Literal`s (`schemas/core.py:937-940`, `:893-895`). The
  registry builds them as literal records (`inference_registry.py:243-390`),
  and the reviewer has a sixth, `external:openai-compatible`
  (`agents/dialectic_reviewer/host_list.py:73`).
- **Families are defined three times, and disagree.** `consult` has
  `_THOROUGH_PEERS` and a substring `_family_of` (`consultation.py:122-127`,
  `:213-223`), the review gate has `_FAMILY_TOKENS` (`review_gate.py:1497-1501`),
  and the reviewer has its own table (`host_list.py:57-77`). Antigravity is
  `google` to `consult`, `unknown` to the reviewer, and resolved by model in
  the gate's records (`review_gate.py:1510-1513`).
- **A self-hoster can configure one local endpoint and nothing else.** Adding
  any provider, including a second OpenAI-compatible server with a key, means
  editing code (`docs/manual/02-install.md:116-117`).

On this deployment, enabling Claude as a reviewer took a pull request (#2814),
and adding GLM through Claude Code would take code changes in about eight
places. An operator with different subscriptions cannot express them at all.

## 2. Proposal

### 2.1 One parser, one vocabulary, two files

One small module defines the vocabulary (2.3) and parses the file format below.
Every caller imports it. Two files use the format:

- **The server's operator file**, `UNITARES_INFERENCE_HOSTS_FILE` (the name
  `dialectic-reviewer-hosts-v0.md` 2.4 reserved). It declares hosts and the
  server's jobs. With no file, behavior is today's.
- **The review gate's repository file**, tracked in the repository, because the
  gate runs in developer checkouts and cloud sessions that cannot see the
  server's file or its Redis (`review_gate.py:70-90`, `:1783-1789`). It names
  the `pr_review` job and replaces `review_providers.json` on the alias
  schedule of `local-inference-one-endpoint-v0.md` 2.1.1. Each side keeps its
  own health state (2.6).

```yaml
version: 1
hosts:
  external:cirwel/glm:                # operator-declared; reviewer-design id form
    kind: cli
    adapter: claude_code               # how to run it and parse its output
    model: glm-5.3
    family: zhipu                      # family of the pinned model
    env: { ANTHROPIC_BASE_URL: "https://api.z.ai/api/anthropic" }
    secrets: { ANTHROPIC_API_KEY: ZAI_API_KEY }   # names only; see 2.5
    egress_class: public_only
    may_approve: false
  external:cirwel/vllm-lab:
    kind: openai_http
    base_url: "https://vllm.lab.internal/v1"
    model: qwen3-coder
    family: other:qwen
    api_key_env: VLLM_LAB_KEY
    egress_class: agent_work
jobs:
  consult.thorough:   { mode: first_answer, prefer: [claude:host-adapter, codex:host-adapter, antigravity:host-adapter] }
  dialectic_reviewer: { mode: first_answer, prefer: [codex:host-adapter, antigravity:host-adapter] }
```

```yaml
# repository file for the review gate
version: 1
jobs:
  pr_review: { mode: collect_families, prefer: [codex, claude, antigravity] }
disabled: {}          # what review_providers.json holds today
```

### 2.2 Host kinds

- **`builtin`**: `ollama:local`, `hf:router` and the three host adapters. They
  stay in code, because each has its own client and parser (codex's
  app-server with its exec fallback, antigravity's resumable client, claude's
  JSON envelope; `host_adapter.py:70-99`) and the first two derive their
  privacy and availability live (`inference_registry.py:249-299`). The file
  can switch a builtin off, set its family per model, and list it in jobs; it
  cannot redefine how it runs.
- **`openai_http`**: any OpenAI-compatible server, declared by the operator.
- **`cli`**: a declared host run through one of the existing adapters
  (`claude_code`, `codex`, `antigravity`) with operator settings. A declared
  `cli` host always runs with tools off and no session persistence, so an
  untrusted prompt cannot act. There is no free-form command line.

### 2.3 One vocabulary

| Field | Values | Notes |
|---|---|---|
| `family` | `anthropic`, `openai`, `google`, `meta`, `zhipu`, `other:<slug>`, `unknown` | from the reviewer design 2.3 |
| `egress_class` | `public_only`, `agent_work`, `operator_private` | what content a host may receive; new field, declared |
| `privacy_class` | unchanged | where the model runs; still derived, never declared |
| `cost_class` | unchanged | display only |

**Family belongs to the model served, as the reviewer design says.** A host
that can serve several families (antigravity serves Claude, GPT-OSS and Gemini
models) declares a family per pinned model; without a pinned model its family
is `unknown`. Where a host reports `model_used`, a mismatch with the declared
family is recorded and any approval from it is withheld. `other:<slug>` never
establishes independence between two operators, because slugs are not
coordinated; for that purpose it counts as `unknown`.

`privacy_class` and `cost_class` keep their current values and meanings, so
code that branches on them (`consultation.py:780`, `delegated_inference.py:338-399`)
and the audit records that store them do not change. `egress_class` is added
beside them.

### 2.4 Jobs: ordered lists the system may only shorten

Jobs are a closed set the code defines; an operator cannot invent one. Each has
a mode:

- **`first_answer`** (`consult.thorough`, `dialectic_reviewer`): try hosts in
  order; the first that answers ends the list, with the reviewer design's
  rules for what counts as an answer.
- **`collect_families`** (`pr_review`): walk the list in order and collect
  passing reviews until the required number of distinct families is met, read
  from `review_policy.json` (two on sensitive paths, one elsewhere). A second
  pass from a family already counted does not count. If the list runs out
  first, the job reports which requirement is unmet.

In both modes the system tries hosts in the operator's order and may only skip
one, for a reason from a closed set: `disabled`, `unconfigured`, `cooldown`,
`excluded_family`, `egress`. It never adds a host, reorders the list, or
chooses by cost, latency, quality or past verdicts, and a test asserts the
order is never permuted. When every host is skipped, the job fails with each
skip and its reason; it never widens to a host the operator did not list. Each
job keeps the reviewer design's cap of three hosts.

**Excluded family:** `consult.thorough` excludes the caller's family and
`dialectic_reviewer` the paused agent's. When that family is `unknown`, nothing
is excluded and the result records `independence: unverified`. `pr_review`
has no exclusion today and gains one here (decision 7.3).

**The shipped defaults reproduce today's order.** `consult.thorough` defaults
to claude, codex, antigravity with the caller's family excluded, which matches
`_THOROUGH_PEERS` (`consultation.py:122-127`) for every caller family including
`unknown`. To keep that true under 2.3, the shipped defaults declare
antigravity's default model as `google`; a request that pins another model on
antigravity takes that model's family. The local lane
is not a job: `consult.standard` and `call_model` keep the one primary endpoint
and one optional fallback of the one-endpoint design.

### 2.5 Secrets and the spawn path

Today the server sends each spawn to the orchestrator as JSON, with the
environment as a string map (`host_adapter.py:730-797`), and the adapter blanks
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY` so a
subscription lane cannot silently become metered (`:770-793`).

- **Values never cross the wire.** The spawn request names secrets; the
  orchestrator resolves each name from its own environment when it starts the
  child, as `local-inference-one-endpoint-v0.md` 2.6 requires. Step 4's
  evidence is "absent from the spawn request", not only "absent from logs".
- **Scrub, then set.** The adapter blanks every credential variable first, then
  sets only the names the host declares. A declared host cannot inherit the
  operator's own keys.
- **The prompt stays in `HA_PROMPT`.** No prompt file.
- **The orchestrator allows only adapter binaries.** Its command allowlist is
  unset today (`elixir/agent_orchestrator/config/config.exs:63`), so any command
  is allowed on the authenticated surface. Step 1 sets it to the binaries the
  adapters use, and a declared `cli` host can only name one of those adapters.

### 2.6 Health state stays with its runner

Cooldowns are not unified. The server keeps its Redis store
(`host_availability.py:85`) and the gate keeps its per-checkout file
(`review_gate.py:2026-2028`); each decides eligibility for the calls it makes.
Sharing would put two runners with different reachability behind one store
that one of them often cannot reach.

### 2.7 What agents see

`consult` stays the front door; `call_model`, `delegate_inference` and the
discovery tools stay hidden.

- **`host_id` becomes a string checked against the loaded registry.** Every
  current id is still accepted. A pinned host must belong to the job's list
  and passes the same egress check; pinning skips only the family exclusion,
  as today. An unknown id fails with the valid ids.
- **Every result names what ran:** `host_id`, `family`, `model_used` (or
  `null` with `model_declared`), and `skipped`, each with a reason code and a
  sentence.
- **A failure explains itself:** "codex: cooldown until 14:20; claude:
  excluded_family (yours); antigravity: unconfigured."
- **Discovery shows the file:** `list_inference_hosts` adds `family`,
  `egress_class`, `enabled` and the jobs that use each host. The fields are
  added, not renamed.

### 2.8 Federation

UNITARES is single-operator; the design records enough for a later federation
to interpret what happened, without claiming federation trust.

- **Operator-scoped.** Only the operator's file declares hosts. A tool
  argument, project file or peer server cannot add one, as Codex ignores
  providers set in a project-local config.
- **Recorded authority.** Every call's provenance records the host id, the
  operator principal, the credential's variable name and a digest of the
  loaded file, so an imported record can be read against the policy that
  selected it.
- **No borrowed compute.** The MCP specification deprecated sampling on
  2026-07-28, so UNITARES does not ask a caller's client for a model, and a
  peer server's hosts are not callable here.
- **Out of scope:** authenticating another operator, or globally meaningful
  host ids. Namespacing prevents collisions; it does not establish trust.

### 2.9 Configuration errors fail closed

A missing file means today's behavior. A file that does not load (bad syntax,
unknown field, unknown family, a secret value where a variable name belongs, a
duplicate id, a file writable by another user) makes every job it would
configure refuse with a named error, and the doctor reports the line. The
server does not fall back to the shipped defaults, which could re-enable a host
the operator switched off. `unitares doctor --inference` prints the effective
configuration, where it came from, and each job's candidate order with the skip
reasons as of now.

## 3. Non-goals

- No router: the operator orders every list.
- No change to the local lane.
- No metered host in any shipped default.
- No secret values in either file.
- No MCP sampling, and no federation trust.
- No free-form commands: a declared `cli` host uses an existing adapter.

## 4. Staging

Each step is its own pull request and preserves behavior for an install that
sets nothing.

1. **Vocabulary, parser, doctor, and the orchestrator allowlist.** The parser
   with fail-closed validation, the `doctor --inference` view, and the
   allowlist set to the adapter binaries. The registry reads families from the
   vocabulary; `list_inference_hosts` gains fields additively, and a parity test
   compares the existing keys.
2. **`consult` and `delegate_inference` read the `consult.thorough` job.**
   `host_id` becomes a checked string: a minor contract bump, since every old
   id is still accepted (`docs/INTERFACE_CONTRACT.md:111-115`), with
   `docs/interface-contract.v1.json` regenerated. `_THOROUGH_PEERS` gives way to
   the job. Results gain `family`, `model_declared` and `skipped`, added to
   `_SAFE_PROVENANCE_FIELDS` and the SDK models.
3. **The reviewer reads `dialectic_reviewer`.** The reviewer design's remaining
   steps (cooldowns, independence, declared hosts) use this file;
   `UNITARES_DIALECTIC_REVIEWER_HOSTS` becomes an alias.
4. **Declared hosts and self-hosting docs.** `openai_http` and `cli` hosts,
   secret resolution in the orchestrator, the install manual, `.env.example`
   and Compose, with a minimal local-only example file and its JSON Schema.
5. **The review gate reads its repository file** with `collect_families`;
   `review_providers.json` becomes an alias on the one-endpoint design's
   schedule. `review_providers.json` is named in the shared contract in both
   `CLAUDE.md` and `AGENTS.md`, so this step updates both.

## 5. Evidence required before each step ships

- Step 1: a file with each listed error is refused with its line; a missing
  file changes nothing; the allowlist refuses a command outside it; the parity
  test passes.
- Step 2: for each caller family including `unknown`, the default order matches
  `_THOROUGH_PEERS`; a Claude-family caller's call skips claude and says so;
  with every host cooling down, the call fails with all reasons and calls
  nothing; the order is never permuted. Tests that pin today's behavior stay
  green: `test_host_adapter.py`, `test_consultation.py`,
  `test_delegated_inference.py`, `test_model_inference.py`,
  `test_local_model_privacy.py`, `test_interface_contract.py`.
- Step 3: the reviewer design's evidence list.
- Step 4: a declared `cli` host answers end to end; its secret's value is
  absent from the spawn request, logs and provenance; a declared host does not
  receive the operator's own keys.
- Step 5: a sensitive-path pull request collects two distinct families in list
  order; a second pass from one family does not count; `test_review_gate.py`
  stays green.

## 6. Risks

- **Wider reach for declared hosts** (decision 7.1). `egress_class` bounds what
  each may receive.
- **Contract change.** `host_id` loses its enum in the published schema;
  strict clients may have cached it.
- **Data leaving the machine.** A host whose retention is unknown is declared
  `public_only`. For pull requests, what counts as public follows the
  repository's visibility, not this repository's.

## 7. Decisions (operator, 2026-10-15)

The operator accepted each recommendation below ("proceed all", 2026-10-15,
after the four questions were put to him with recommendations).

1. **Declared hosts beyond the reviewer: yes.** `consult.thorough` and
   `pr_review` may list declared hosts, bounded by `egress_class`. This widens
   `dialectic-reviewer-hosts-v0.md` 2.4, which kept declared hosts reachable from
   the reviewer list only.
2. **GLM's egress class: `public_only`.** Z.ai's terms do not state prompt
   retention, and its servers are under PRC jurisdiction, so GLM receives only
   content that is already public, such as review of this public repository.
3. **The author's family is excluded from `pr_review`,** taken from the family the
   pull request declares; an undeclared family counts as `unknown` and excludes
   nothing. The source is recorded with each review.
4. **Job orders for this deployment:** today's orders, with GLM appended last to
   `pr_review` once declared hosts exist (step 4).
