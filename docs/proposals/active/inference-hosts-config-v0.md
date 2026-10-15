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

*Rewritten 2026-10-15 (decision 7.5), then revised after an adversarial design
review the same day. The first text proposed an allowlist matched on a
command's leading arguments; it would not have bounded a caller holding the
bearer token.*

**Threat model.** Profiles defend against two callers: a relay that passes on
input an attacker influenced (the governance server, the lease plane,
dispatch_beam), and a holder of the orchestrator's bearer token alone (a
leaked token, or a sandboxed agent that can reach localhost but cannot execute
code). They do not bound a process running as the operator's user, which can
read the secrets file and run anything directly, nor a holder of the node's
Erlang cookie, which gives code execution inside the orchestrator. The cookie
is kept off every command line (#2825); `AgentOrchestrator.run/1` stays
unrestricted because only code inside the VM can call it.

**What a caller controls today.** `POST /v1/agents` (`http_router.ex`
`build_spec`) takes `cmd`, `args`, `env`, `cd`, `stdin`, `lease`, `lineage`,
`server_url`, `client_session_id`, `max_output_lines` and `max_runtime_ms`.
The orchestrator merges `env` over its own environment, which includes every
exported line of the secrets file, applies `cd` unchecked, and turns `lineage`,
`server_url` and `client_session_id` into child variables. The three host
adapters launch through `/bin/sh -c`; the reviewer launches as
`python -m agents.dialectic_reviewer` with a forwarded server environment
that includes `UNITARES_MCP_BEARER_TOKEN`, the CLI paths, the local model
endpoint and the classifier settings; dispatch_beam's council lanes launch
`claude` and `codex` directly with the prompt in `argv`. A command allowlist,
even one matched on leading arguments, leaves the bearer holder able to run
code through an allowed command (`HOME`, `NODE_OPTIONS` or `PYTHONPATH` in
`env`; a `cd` holding the caller's package or project configuration; an
appended flag; a CLI path variable) and to route any secret in the
orchestrator's environment to a host of its choosing (the reviewer's external
endpoint and the *name* of its key variable are both caller-supplied, and
`UNITARES_GOVERNANCE_URL` receives the MCP bearer token).

**Profiles.** The orchestrator spawns only named profiles the operator
defines. A profile fixes everything a caller could use to change what runs:

| Field | Meaning |
|---|---|
| `cmd` | absolute path of the executable |
| `args` | the complete argument list; no placeholders |
| `cd` | a fixed absolute path |
| `env.inherit` | names copied from the orchestrator's environment (default `HOME`, `PATH`, `LANG`, `TMPDIR`); every other inherited variable is removed |
| `env.set` | fixed values, including `USER` (launchd strips it) and the blanked credential variables of today's adapters |
| `env.secrets` | child variable name → orchestrator variable name, resolved when the child starts |
| `vars` | the variable names a caller may supply, each with a byte limit, an optional pattern, and `required` |
| `lineage` | `none`, or `caller` (the governed path's `parent_agent_uuid` and `spawn_reason`) |
| `server_url`, `lease` | fixed, or absent |
| `max_runtime_ms`, `max_output_lines` | ceilings; a request may ask for less, never more |
| `max_concurrent` | how many children of this profile may run at once |

The environment is built in one order: remove everything not inherited, set
the fixed values, resolve the secrets, add the caller's variables, then add
the orchestrator's own provisioned variables. A caller variable may never name
a fixed, secret or provisioned variable. Variables the launching shell itself
adds (`PWD`, `SHLVL`, `_`) are allowed in the child.

**Requests are a closed schema:** `{"profile": "...", "vars": {...},
"max_runtime_ms": n}` plus the existing `Idempotency-Key` header, and
`lineage` only where the profile accepts it. Any other field is refused by
name. An unknown variable, a missing required one, or a value over its limit
or outside its pattern is refused with the variable's name and never its
value. Model variables use `^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$` (no
leading `-`); for a metered host the profile fixes the model or enumerates the
allowed ones, since spending is the operator's decision. Every refusal happens
before an idempotency reservation; the reservation digest for a profile
request covers the profile name, the sorted variables, the effective ceilings,
the effective lineage and a digest of the profile's non-secret fields, so a secret rotation does not
turn a retry into a conflict, and raw requests keep today's digest unchanged.

**Every profile runs a client, and the client owns the prompt.** No profile
takes a prompt or a model in its arguments. Each runs a small stdlib-only
client with the interpreter's isolated mode (`-I`, which ignores `PYTHON*`
variables and the working directory): the existing codex app-server and
antigravity clients, a new `claude_cli_client.py`, and council clients for
dispatch_beam's lanes. The client reads the prompt and options from the
profile's variables, validates them, and starts the CLI with the prompt on
standard input where the CLI reads it there (`claude -p`, `codex exec -`);
`agy` takes it as one argument behind a fixed guard prefix. A caller
directory, where a lane needs one (the council reviews a repository), is a
variable the client resolves with `realpath` and accepts only under roots the
profile fixes; the orchestrator itself never takes `cd` from a caller. The
orchestrator therefore needs no way to write a child's standard input (Erlang
ports cannot half-close it), and the earlier `stdin_var` field is dropped.
Environment variables are not secret from other processes of the same user
(`ps -E`); they are used because they are not parsed as options.

**The dialectic reviewer takes one variable: the session id.** It fetches the
thesis, the paused agent's state and family, and its parent from the session
record (`dialectic get`), and resolves its host list from the
`dialectic_reviewer` job itself, reading the inference hosts file the profile
names. A caller that could supply the thesis, the paused agent's state or the
host list could have a reviewer with its own identity file a verdict on a real
session from invented evidence, misstate the family the reviewer must exclude,
or pick a metered host. Everything the reviewer sends a secret to, or reads a
policy from, is fixed in the profile: the governance URL and MCP bearer token,
the CLI paths, the local model endpoint, the classifier settings
(`UNITARES_MODEL_LOCAL_HOSTS`, `UNITARES_MODEL_PRIVACY`,
`UNITARES_TRUSTED_NETWORKS`), and each external host's base URL and key. The
reviewer's launch adds `-s`, so user-site `.pth` files do not load.

**Secrets bind to profiles, not requests.** A caller chooses among profiles;
it cannot point a profile's secret elsewhere. Every `env.secrets` source is
checked when the profiles load and again at spawn: an unset or empty source
refuses the spawn by name, never runs without it. The orchestrator redacts the
resolved values of a child's secrets from the output it captures and returns.
Requests now carry no secrets, so neither does the lease plane's
governed-effect payload, which carries the request; that plane's credential
check reads only top-level keys, so a bearer inside `env` passes it today (no
persisted copy was found on 2026-10-15).

**Declared `cli` hosts do not see the operator's logins.** A `claude_code`
host whose `ANTHROPIC_BASE_URL` is a third party, running with the operator's
`HOME`, falls back to the operator's subscription login when its bound key is
empty, and sends it to that URL. A declared `cli` host's profile therefore
runs with an isolated `HOME` (the antigravity client's pattern, without the
keychain link), or with `claude --bare`, which never reads OAuth or the
keychain, and with an isolated `CODEX_HOME` for the codex adapter.

**Codex lanes are confined** (#2827, cirwel/dispatch_beam#87). Codex's legacy
`--sandbox read-only` reads the whole disk, so every codex lane runs with a
deny-read permission profile, core-only command environment, no MCP servers,
hooks, plugins, apps, browser or computer use, an empty working directory
(or, for the council, the reviewed workspace minus `.env` files), and refuses
to run when administrator codex configuration exists. A profile that launches
codex must keep these.

**Where profiles come from.** The orchestrator reads
`AGENT_ORCHESTRATOR_PROFILES_FILE` (JSON). It refuses a file, or a parent
directory, that another user owns or can write. Without the file, profile
spawns are off and raw spawns behave as today. A file that does not validate
at boot disables profile spawns, with the reason on the authenticated
`/v1/health`; a replacement that does not validate while running leaves the
last good set in force and reports the error there. The orchestrator reloads
when the file's digest changes, so regenerating it needs no restart (a
restart kills running reviewers, which can live for hours).

`scripts/ops/orchestrator-profiles.py` writes the file, and
`scripts/start.sh` runs it before boot. It reads files only: the server's
installation (interpreter, repository root, which must be the deploy tree,
never a worktree agents edit), the inference hosts file, and the resolved CLI
paths, pinning each CLI's symlink rather than its target (claude's
auto-update replaces the target). The reviewer's external host moves into the
hosts file as a declared `openai_http` host, and the
`UNITARES_DIALECTIC_EXTERNAL_*` variables become aliases for it, so the
generator never depends on the server's process environment. Declared `cli`
hosts become generated profiles. The doctor compares the loaded profiles'
digest, reported on `/v1/health`, with a fresh generation; the server refuses
to dispatch while they differ, because a stale profile is a stale egress
policy.

**Raw spawns.** `AGENT_ORCHESTRATOR_RAW_SPAWN` is `allow` or `deny`. It
defaults to `deny` whenever `AGENT_ORCHESTRATOR_PROFILES_FILE` is set, whether
or not the file exists or validates, and to `allow` only when it is unset: a
missing or broken profiles file must never reopen raw spawning, so it leaves
the orchestrator spawning nothing until it is fixed. The mode is shown on
`/v1/health`, and the doctor warns on an explicit `allow` beside a profiles
file. The basename `cmd_allowlist` stays as a guard on raw spawns during
migration and is retired with them. A server or dispatch_beam configured for
profiles never falls back to a raw spawn or to running a lane in-process when
a profile spawn is refused; the refusal is the result.

**One bearer, all profiles.** Any holder of the orchestrator's token may start
any profile, including the reviewer. That is bounded by the reviewer taking
only a session id (starting it is equivalent to requesting the review the
session already needs), by `max_concurrent`, and by each profile's ceilings.
Scoping profiles to separate tokens per caller is left for later.

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
     validation and digest reload, the closed request schema, environment
     building in the stated order, `env.secrets` checked at load and at
     spawn, output redaction, ceilings and `max_concurrent`, the profile
     request digest, and `AGENT_ORCHESTRATOR_RAW_SPAWN` (`deny` whenever a
     profiles file is configured, valid or not; `allow` only without one). No profiles file ships yet, so nothing changes.
   - **4b. The clients, the generator and the callers.** The claude and
     council clients (the codex and antigravity clients exist); the
     reviewer reads its thesis, state, family, parent and host list from the
     server and the hosts file; the external reviewer host becomes a declared
     `openai_http` host with environment aliases; the generator, run by
     `start.sh`; the host adapters, the reviewer dispatch and the lease
     plane's governed path (`governed_spawn.py` and `orchestrator_spec`
     together) send `{profile, vars}`; the doctor's digest check. Deploy
     order: orchestrator and lease plane before the server. dispatch_beam's
     council moves in its own pull request, with its direct fallback removed.
     Writing the profiles file is what turns raw spawns to `deny`.
   - **4c. Raw spawns retire:** after a soak with per-profile spawn counts
     non-zero and fallback counts zero, `cmd_allowlist` and the raw path are
     removed. An effect proposed before the migration and replayed later is
     refused, not run as a raw spawn.
   - **4d. Declared hosts.** `openai_http` and `cli` hosts; each declared
     `cli` host becomes a generated profile with its secrets bound, an
     isolated `HOME` or `--bare`, and an isolated `CODEX_HOME`.
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
- Step 4:
  - each shipped profile still runs (every host adapter, the reviewer on the
    direct and governed paths, both council lanes), in CI against shims and
    in one operator-run live check;
  - a request with any field outside the closed schema is refused by name,
    one test per field; with raw spawns denied, any `cmd` request is refused,
    `/bin/sh` included;
  - a caller variable cannot set an unlisted, fixed, secret or provisioned
    name; an inherited variable not in `env.inherit` is absent from the
    child; a missing secret refuses the spawn;
  - a secret's value is absent from the request, the response (redacted
    output), logs, telemetry, the lease plane's payload and provenance;
  - the reviewer, given a real session id and nothing else, reviews the
    session's own thesis; a host list or thesis supplied by the caller has
    no route into it;
  - a council directory outside the profile's roots is refused after
    resolving symlinks; a prompt that begins with `--` reaches the model as
    text;
  - a profile request replays and conflicts under the same idempotency
    rules as today, including a conflict when only its lineage differs, and
    a raw request's digest is unchanged;
  - with a profiles file configured but missing or invalid, a raw request is
    refused;
  - a declared `cli` host answers end to end and receives neither the
    operator's keys nor the operator's logins;
  - after migration, per-profile spawn counts are non-zero and in-process
    fallback counts are zero.
- Step 5: a sensitive-path pull request collects two distinct families in list
  order; a second pass from one family does not count; `test_review_gate.py`
  stays green.

## 6. Risks

- **Wider reach for declared hosts** (decision 7.1). `egress_class` bounds what
  each may receive.
- **Contract change.** `host_id` loses its enum in the published schema;
  strict clients may have cached it.
- **Profiles drift from the server's settings.** A profile pins what a secret
  is sent to, so a stale file is a stale egress policy. `start.sh`
  regenerates it, the orchestrator reloads on a digest change, and the server
  refuses to dispatch while the doctor's comparison differs.
- **Migration breaks a caller.** A refused profile request names the profile
  and the field, and callers configured for profiles do not fall back, so a
  broken caller fails visibly; the per-profile counters show it.
- **What profiles do not bound.** The operator's own user, the node cookie,
  and what a launched agent does with a prompt within its profile (2.5,
  threat model). A codex lane's answer is bounded by its confinement, not by
  the profile.
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
