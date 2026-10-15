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
  the `pr_review` job, may declare the gate's own reviewer hosts in the same
  schema (declared host ids are separate from the server's), and replaces
  `review_providers.json` on the alias schedule of
  `local-inference-one-endpoint-v0.md` 2.1.1. A host's credential comes from
  the environment of the checkout running the gate; where it is absent, the
  host is skipped as `unconfigured`. Each side keeps its own health state
  (2.6).

```yaml
version: 1
hosts:
  external:example/compat-claude:     # operator-declared; reviewer-design id form
    kind: cli
    adapter: claude_code               # how to run it and parse its output
    model: example-model
    family: other:example              # family of the pinned model
    env: { ANTHROPIC_BASE_URL: "https://anthropic-compatible.example/api" }
    secrets: { ANTHROPIC_API_KEY: EXAMPLE_API_KEY }   # names only; see 2.5
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
`first_answer` job keeps the reviewer design's cap of three hosts. A
`collect_families` job may list four, because it needs more than one answer.

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

### 2.5 Secrets and the spawn path: named launch profiles

*Rewritten 2026-10-15 (operator decision 7.5). The earlier text proposed an
allowlist that matched a command's leading arguments. That allowlist would
not have bounded a caller holding the bearer token, for the reasons below.*

**What a caller controls today.** `POST /v1/agents` takes `cmd`, `args`, `env`
and `cd` from the caller (`http_router.ex` `build_spec`). The orchestrator
merges `env` over its own inherited environment and applies `cd` unchecked
(`agent_runner.ex`). The three host adapters launch through `/bin/sh -c`
(`host_adapter.py` `_HOST_COMMANDS`); the reviewer launches as
`python -m agents.dialectic_reviewer` with a forwarded server environment,
including `UNITARES_MCP_BEARER_TOKEN` (`orchestrator_dispatch.py` `_build_spec`);
dispatch_beam's council lanes launch `claude` and `codex` directly
(`dispatch_beam` `council.ex` `orchestrator_spec/2`). A command allowlist,
even one matched on leading arguments, leaves the bearer holder able to run
code through any allowed command:

- `env` sets `HOME`, `NODE_OPTIONS`, `PYTHONPATH` or a CLI's config location,
  so an allowed CLI loads the caller's settings, hooks or modules;
- `cd` points `python -m` or a CLI at a directory holding the caller's package
  or project configuration;
- appended `args` add flags the allowlisted prefix did not anticipate;
- the reviewer's external endpoint and the *name* of its key variable are both
  caller-supplied, so a caller can route any secret in the orchestrator's
  environment to a host of its choosing; `UNITARES_GOVERNANCE_URL` likewise
  receives the MCP bearer token.

**Profiles.** The orchestrator spawns only named profiles that the operator
defines. A profile fixes everything except the caller's variables:

| Field | Meaning |
|---|---|
| `cmd` | absolute path of the executable |
| `args` | the complete argument list; no placeholders |
| `cd` | a fixed absolute path, or `within: [roots]`, which accepts a caller directory only if its resolved real path lies under one of the roots |
| `env.inherit` | names copied from the orchestrator's environment; **every other inherited variable is removed** |
| `env.set` | fixed values, including the blanked credential variables of today's adapters |
| `env.secrets` | child variable name → orchestrator variable name, resolved when the child starts; the value never appears in a request, response, log or effect payload |
| `vars` | the variable names a caller may supply, each with a byte limit, an optional pattern and `required` |
| `stdin_var` | a caller variable whose value is written to the child's standard input, which is then closed |
| `max_runtime_ms` | the ceiling; a request may ask for less, never more |

A request names a profile and supplies variables:
`{"profile": "host.claude", "vars": {"PROMPT": "...", "MODEL": "..."}}`. A request
that also carries `cmd`, `args`, `env` or `cd` is refused. An unknown variable,
a missing required one, or a value over its limit or outside its pattern is
refused with the variable's name and never its value.

**Prompts never travel in `argv`.** A prompt in `argv` is an option-injection
surface (a prompt beginning with `--` is parsed as a flag), is capped by the
system's argument limit, and is visible to every process listing. Each profile
delivers the prompt through `stdin_var` or through an environment variable read
by a client script. `claude -p` and `codex exec -` read the prompt from standard
input; the codex app-server client and the antigravity client already read
`HA_PROMPT`.

**Secrets bind to profiles, not requests.** Everything a secret is sent to, and
every secret's name, is fixed in the profile: the reviewer's governance URL and
MCP bearer token, its external endpoint and that endpoint's key, a declared
host's base URL and key. A caller can choose among profiles; it cannot point a
profile's secret somewhere else. This also keeps secrets out of the lease
plane's governed-effect payload, which carries the request; that plane's
credential check reads only top-level payload keys, so a bearer inside `env`
would pass it today (no persisted copy was found on 2026-10-15).

**Where profiles come from.** The orchestrator reads
`AGENT_ORCHESTRATOR_PROFILES_FILE` (JSON) at boot. It refuses a file another
user owns or can write, and a file that does not validate disables profile
spawns with the reason on `/v1/health`. A generator,
`scripts/ops/orchestrator-profiles.py`, writes the file from the installation:
the server's interpreter and repository root, the resolved CLI paths
(`resolve_host_cli`), the governance URL, and the server's reviewer settings.
The deploy runs it, and a self-hoster runs it once. Declared `cli` hosts in the
inference hosts file become generated profiles (step 4d), so the operator keeps
one source; the orchestrator never reads that file itself.

**Raw spawns during migration.** `AGENT_ORCHESTRATOR_RAW_SPAWN` is `allow`
(the default until every caller has moved) or `deny`. With `deny`, a request
carrying `cmd` is refused. Turning it to `deny` on this deployment is an
operator act that needs an orchestrator restart. The in-VM
`AgentOrchestrator.run/1` is unchanged, since its callers already run inside
the VM.

**The profiles this repository ships** (generated, then reviewed by the
operator):

| Profile | Launch | Caller variables |
|---|---|---|
| `host.codex` | interpreter `-I` + `codex_app_server_client.py`, which now owns its exec fallback (`codex exec -` with the prompt on standard input) | `HA_PROMPT`, `HA_MODEL` (model pattern), `HA_TIMEOUT_S` (digits), `HA_SANDBOX` (`read-only`), `HA_CODEX_APP_SERVER` (`0`/`1`) |
| `host.claude` | interpreter `-I` + `claude_cli_client.py` (below), which starts `claude --safe-mode -p --tools "" --no-session-persistence --output-format json` with the prompt on standard input | `HA_PROMPT`, `HA_MODEL` (model pattern), `HA_TIMEOUT_S` |
| `host.antigravity` | interpreter `-I` + `antigravity_cli_client.py` | `HA_PROMPT`, `HA_MODEL`, `HA_TIMEOUT_S` |
| `dialectic.reviewer` | interpreter `-m agents.dialectic_reviewer`, `cd` = repository root, `PYTHONPATH` fixed | session id, thesis fields, paused-agent state, parent id, the resolved host list, timeouts and model ids; never a URL, key or key name |
| `council.claude`, `council.codex` | dispatch_beam's lane flags, prompt on standard input; codex's `--profile` is dropped, because it selects arbitrary local configuration | `PROMPT`, `MODEL` (codex only, pattern), `cd` `within` the operator's project roots |

`host.claude` uses a client because its optional model cannot be a fixed
profile argument (profiles have no placeholders). `claude_cli_client.py` reads
`HA_PROMPT` and `HA_MODEL`, checks the model against the pattern, and starts
`claude` with the prompt on standard input. Every host adapter then has the
same shape: a stdlib-only client run with the interpreter's isolated mode,
which ignores `PYTHON*` variables and the working directory.

**A caller directory is still an input.** A profile that accepts `cd` within
the operator's roots must also pin the flags that stop its CLI from loading
configuration from that directory. Which flags achieve that for `claude` and
`codex` is verified per CLI version before the council profiles ship, and the
profile documents the result.

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

- **Operator-scoped.** Only operator-controlled files declare hosts: the
  server's operator file, and for the review gate alone, the tracked repository
  file, which changes only through reviewed pull requests. A tool argument, an
  untracked project file or a peer server cannot add one, as Codex ignores
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
duplicate id, a job naming an id declared nowhere, a file writable by another
user) makes every job it would configure refuse with a named error, and the
doctor reports the line or field at fault. The server does not fall back to the
shipped defaults, which could re-enable a host the operator switched off. The
doctor's `inference_hosts_file` check prints the effective configuration, where
it came from, and each job's host order.

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

1. **Vocabulary, parser and doctor.** The parser with fail-closed validation and
   the doctor's `inference_hosts_file` check. The registry reads families from
   the vocabulary; `list_inference_hosts` gains `family` additively (the other
   new fields arrive with the routing that honours them, so discovery never
   shows a setting routing ignores), and a parity test compares the existing
   keys.
2. **`consult` and `delegate_inference` read the `consult.thorough` job.**
   `host_id` becomes a checked string: a minor contract bump, since every old
   id is still accepted (`docs/INTERFACE_CONTRACT.md:111-115`), with
   `docs/interface-contract.v1.json` regenerated. `_THOROUGH_PEERS` gives way to
   the job. Results gain `family`, `model_declared` and `skipped`, added to
   `_SAFE_PROVENANCE_FIELDS` and the SDK models.
3. **The reviewer reads `dialectic_reviewer`.** The reviewer design's remaining
   steps (cooldowns, independence, declared hosts) use this file;
   `UNITARES_DIALECTIC_REVIEWER_HOSTS` becomes an alias.
4. **Launch profiles, declared hosts and self-hosting docs** (2.5), in parts:
   - **4a. The orchestrator spawns profiles.** The profiles file, its
     validation, `{profile, vars}` requests, environment scrubbing,
     `env.secrets`, `stdin_var`, `cd within`, and `AGENT_ORCHESTRATOR_RAW_SPAWN`
     defaulting to `allow`. No caller changes, so nothing moves yet.
   - **4b. Callers move to profiles.** The host adapters (with the codex
     client owning its exec fallback and the new claude client), the reviewer
     dispatch and the lease plane's governed path send `{profile, vars}`; the
     generator writes the file; the deploy runs it. A server configured for
     profiles never falls back to a raw spawn. dispatch_beam's council lanes
     move in their own repository's pull request.
   - **4c. The operator denies raw spawns** on this deployment
     (`AGENT_ORCHESTRATOR_RAW_SPAWN=deny`, orchestrator restart).
   - **4d. Declared hosts.** `openai_http` and `cli` hosts; each declared
     `cli` host becomes a generated profile with its secrets bound.
   - **4e. Self-hosting docs:** the install manual, `.env.example` and
     Compose, a minimal local-only example file, and the JSON Schemas for
     both files.
5. **The review gate reads its repository file** with `collect_families`;
   `review_providers.json` becomes an alias on the one-endpoint design's
   schedule. `review_providers.json` is named in the shared contract in both
   `CLAUDE.md` and `AGENTS.md`, so this step updates both.

## 5. Evidence required before each step ships

- Step 1: a file with each listed error is refused with its line or field; a
  missing file changes nothing; the parity test passes.
- Step 2: for each caller family including `unknown`, the default order matches
  `_THOROUGH_PEERS`; a Claude-family caller's call skips claude and says so;
  with every host cooling down, the call fails with all reasons and calls
  nothing; the order is never permuted. Tests that pin today's behavior stay
  green: `test_host_adapter.py`, `test_consultation.py`,
  `test_delegated_inference.py`, `test_model_inference.py`,
  `test_local_model_privacy.py`, `test_interface_contract.py`.
- Step 3: the reviewer design's evidence list.
- Step 4: each shipped profile still runs (every host adapter, the reviewer on
  the direct and governed paths, both council lanes); a request carrying
  `cmd`, `args`, `env` or `cd` beside a profile is refused, and with raw spawns
  denied so is any `cmd` request, `/bin/sh` included; a caller variable cannot
  set an unlisted name, and an inherited variable not in `env.inherit` is absent
  from the child; a secret's value is absent from the request, the response,
  logs, the lease plane's payload and provenance; a `cd` outside a profile's
  roots is refused after resolving symlinks; a prompt that begins with `--`
  reaches the model as text; a declared `cli` host answers end to end and does
  not receive the operator's own keys.
- Step 5: a sensitive-path pull request collects two distinct families in list
  order; a second pass from one family does not count; `test_review_gate.py`
  stays green.

## 6. Risks

- **Wider reach for declared hosts** (decision 7.1). `egress_class` bounds what
  each may receive.
- **Contract change.** `host_id` loses its enum in the published schema;
  strict clients may have cached it.
- **Profiles drift from the server's settings.** A profile pins what a secret
  is sent to (URLs, key names), so changing one of those server settings
  without regenerating the file leaves the old value in force. The deploy
  regenerates it, and the doctor compares the loaded profiles' digest with a
  fresh generation.
- **Migration breaks a caller.** Raw spawns stay allowed until every caller has
  moved and the evidence above passes; a refused profile request names the
  profile and the field, so a broken caller fails visibly rather than falling
  back.
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
   `pr_review` once the gate reads its repository file (step 5). GLM is declared
   in that file and runs where `ZAI_API_KEY` is set; elsewhere it is skipped as
   `unconfigured`.

5. **Step 4 builds named launch profiles** (2.5), chosen by the operator on
   2026-10-15 from three options put to him: profiles, the leading-argument
   allowlist as first written, or direct launch with the bearer token as the
   only boundary. Profiles were chosen because the allowlist would not bound
   a caller holding the token. Recorded in the knowledge graph as
   `2026-10-15T10:29:57.051370+00:00`.

**Amendment (operator, 2026-10-15, later the same day):** GLM is phased out;
the operator ended the trial and the subscription. Decisions 2 and 4 no longer
apply to this deployment: no GLM host is declared, and `pr_review` keeps
today's order. Decision 2's reasoning still describes how to classify any host
whose retention terms are unstated. The section 2.1 example now uses a
placeholder host.
