# One local model endpoint (v0)

Status: Proposed, design-only. The operator's decisions on the three open
questions are recorded in section 7 (2026-09-27). Merging this document does
not by itself authorize the build.

Date: 2026-09-27

## 1. Problem

UNITARES asks a model for help in several places, and a fresh install must be
able to point all of them at the model it already has. Today that works only
if the model is served by Ollama.

Paths in the server that call a local model (all in `src/mcp_handlers/`):

| Path | Entry | Client today |
|---|---|---|
| `call_model` | `support/model_inference.py` | async OpenAI client on Ollama `/v1`, Hugging Face router as fallback |
| `consult(effort='standard')` | `support/consultation.py` via `run_model_inference` | same as `call_model` |
| In-process dialectic reviewer | `dialectic/handlers.py` via `support/llm_delegation.py` | native Ollama `/api/chat` with a JSON schema, then a sync OpenAI client on Ollama `/v1` |
| `dialectic(reviewer_mode='llm')` | `dialectic/handlers.py` | sync OpenAI client on Ollama `/v1` |
| Check-in enrichments on guide or pause | `updates/enrichments.py` via `llm_delegation.py` | sync OpenAI client on Ollama `/v1` |
| Knowledge synthesis and audit | `knowledge/synthesis.py`, `src/knowledge_graph_lifecycle.py` | `call_local_llm` or `call_model` |

Outside the server, two agent processes build their own clients on the same
Ollama base with a hard-coded `"ollama"` key: the orchestrated dialectic
reviewer's `local` backend (`agents/dialectic_reviewer/reviewer.py`) and the
local resident runner (`agents/local_resident/runner.py`).

Out of scope: the Watcher resident (`agents/watcher/agent.py`) reads its own
`WATCHER_OLLAMA_URL` and `WATCHER_MODEL`, and a deployment pins the model it
has self-tested Watcher with. It keeps its settings. Moving it onto the shared
endpoint is a separate decision that needs its own self-test, so "every local-model path" below means the server and the
processes the server starts, plus the local resident runner. Its `external` backend
(`host_backends.py`) already talks to any OpenAI-compatible endpoint, configured
by `UNITARES_DIALECTIC_EXTERNAL_BASE_URL`, `_MODEL` and `_API_KEY_ENV`. That
backend runs only in the orchestrated reviewer, which a default install does
not start.

What this costs an installer:

- **Ollama is assumed, not configured.** The documented settings are
  `UNITARES_OLLAMA_BASE` (an Ollama root URL, without `/v1`) and
  `UNITARES_LLM_MODEL`. The in-process reviewer's first attempt uses Ollama's
  native `/api/chat` route, and availability is a socket probe on the Ollama
  port. An installer whose model runs on vLLM, LM Studio, llama.cpp's server,
  OpenRouter or OpenAI has no supported setting for it outside the
  orchestrated reviewer.
- **Two vocabularies for the same choice.** `call_model` takes
  `provider=auto|hf|ollama` and `privacy=local|auto|cloud`. `consult` takes
  `privacy=local|cloud_allowed`.
- **Six client constructions.** In the server: one async, two sync (one of
  them run in an executor), and one stdlib `urllib` call to `/api/chat`. In the
  agent processes: the reviewer's and the resident's. Each has its own timeout
  handling, and every one sends the fixed key `"ollama"`.
- **Many settings.** A survey of `origin/master` on 2026-09-27 counted about 41
  distinct inference environment variables across all lanes, of which the
  install manual names three. Most belong to the orchestrated reviewer and the
  host-adapter extension, which PR #2507 now labels as operator extensions.
  This proposal is about the local lane only.

`src/local_inference_env.py` already gives every local reader one resolver for
the Ollama base and default model, so one setting moves them all. This
proposal generalizes that resolver rather than replacing it.

## 2. Proposal

### 2.1 One endpoint, described the way every server describes itself

The local lane becomes one OpenAI-compatible endpoint:

| Setting | Meaning | Default |
|---|---|---|
| `UNITARES_MODEL_BASE_URL` | Base URL including `/v1` | derived from `UNITARES_OLLAMA_BASE` if set, else `http://localhost:11434/v1` |
| `UNITARES_MODEL` | Model id the endpoint serves | `UNITARES_LLM_MODEL` if set, else none (see 7.2) |
| `UNITARES_MODEL_API_KEY_ENV` | Name of the variable that holds the endpoint's key, if it needs one | `UNITARES_MODEL_API_KEY` |

The key is named indirectly, as the orchestrated reviewer's `external` backend
already does (`UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV`), so a process that must
not carry the value (see 2.6) can be told which variable to read. Ollama,
vLLM, LM Studio, llama.cpp's server, OpenRouter, OpenAI and the Hugging Face
router all accept this shape.

### 2.1.1 Old names expire

Old names are handled by one rule, so they cannot pile up across releases:

- **A name that never shipped gets no alias.** Renaming anything not yet in a
  published release is free. `UNITARES_OLLAMA_BASE` exists only on `master`
  (#2495) and is renamed outright if step 1 lands before the next release;
  otherwise it joins the table below.
- **A shipped name gets an alias only through one table**, in
  `local_inference_env.py`, with three columns: old name, new name, and the
  release that removes it (the release after next). Code reads only the new
  names; the resolver consults the table once. v2.22.1 shipped two:
  `UNITARES_LLM_MODEL` and `UNITARES_OLLAMA_BASE_URL`.
- **CI enforces the date.** A test fails once `VERSION` reaches an entry's
  removal release, so the release cut removes the alias or a reviewed diff
  moves its date. No alias outlives its date silently.
- **Quiet while it lives.** Setting only an old name logs nothing per call.
  The existing one-time warning fires only when an old and a new name are both
  set and disagree. The doctor prints one INFO line per old name in use, with
  the new name and the removal release, and the flag catalog lists it as
  "alias of X until vN".

This matters most at the spawn boundary. The governance server, the
orchestrator service and the reviewer child can run different releases on one
host, and federated operators upgrade on their own schedules. While an alias is
in the table, the dispatcher forwards both the new and the old name (2.6), so
an older child still reads its model and a newer one reads the new name. A
hard rename would break any pairing that straddles it.

The server's own release cannot prove that the orchestrator service has caught
up, so the spawn boundary does not follow the table's removal date. The
dispatcher keeps forwarding an old name until the orchestrator reports a
release at or past that name's removal release (its health response carries
its release; step 1 adds that if it is missing), and forwards both whenever it
cannot read one. Dropping an old name at the spawn boundary is therefore
decided by the child's release, not the server's.

An existing install keeps working with no edits, except that one which relies
on the implicit model must name it before step 4 (section 4).

### 2.1.2 Every setting reaches every process that reads it

The settings in this proposal are: the endpoint, model and key-name settings;
the classifier's `UNITARES_MODEL_LOCAL_HOSTS` and `UNITARES_MODEL_PRIVACY`; the
`UNITARES_MODEL_ALLOW_INSECURE_HTTP` opt-in; the probe timeout; the same set
under `UNITARES_MODEL_FALLBACK_*`; and the key value under its default name,
`UNITARES_MODEL_API_KEY`. A setting that exists but does not reach the process
that reads it is worse than none, because the install looks configured. So
one list in `local_inference_env.py` names them all, and one test checks that
every name on it is:

- mapped for `governance-mcp` in `docker-compose.yml`;
- present in the macOS LaunchAgent template, key value included as a
  placeholder, because a launchd service inherits no shell environment;
- forwarded by the reviewer dispatcher (2.6), except the key value, which is
  never forwarded; the classifier settings are forwarded so the child reaches
  the same `local` or `external` answer as the server.

Each step below adds its names to that list, so the test fails until the
step's Compose, plist and dispatcher entries exist. The steps no longer
enumerate these mappings one by one.

### 2.2 One client

A new `src/local_model_client.py` owns the only construction of an
OpenAI-compatible client for the local lane, with an async call and a sync
wrapper for the paths that are still sync. It imports `openai` lazily. The
resolver in `local_inference_env.py` stays stdlib-only, because agent processes
import it.

Structured output (the in-process reviewer's JSON-schema answers) moves to the
OpenAI-compatible `response_format` with a `json_schema`. Ollama, vLLM, LM
Studio and OpenAI accept it. The native `/api/chat` path stays behind a
detection of an Ollama endpoint until the evaluation in 5.1 shows the
`response_format` route is at least as good on the reviewer's real prompts.

### 2.3 Privacy comes from where the endpoint is

`privacy='local'` means "this data does not leave machines the operator runs".
Today it is enforced as "the route was Ollama". Under this proposal the
server classifies the configured endpoint from the URL alone, never from a DNS
answer:

- **An IP literal** is `local` when it is in the server's existing
  trusted-network list (`_TRUSTED_NETWORKS` in `src/http_routes/access.py`:
  loopback, 100.64.0.0/10 and the RFC 1918 ranges) or is an RFC 4193 private
  address, and `external` otherwise.
- **A hostname** is `local` only when it is `localhost`,
  `host.docker.internal`, or a name the operator lists in
  `UNITARES_MODEL_LOCAL_HOSTS`. Every other hostname is `external`, even one
  that currently resolves to a private address.

Classifying from DNS would leave a check-then-use race: a name with several or
changing answers could be classified from one private answer and then connect
to a public one, so a `privacy='local'` prompt would leave the operator's
machines while the server reported otherwise. Listing a name is the operator's
explicit statement that it stays on their network, and it is the only way a
hostname becomes `local`. The failure mode is therefore a refusal that names
the setting to change, never a silent send.

Reusing that address list keeps one definition of "local" in the server: the
dashboard and WebSocket access checks already trust the same ranges. Tailscale is not
part of UNITARES setup; 100.64.0.0/10 is in the list because an address there
is normally a tailnet peer the operator runs.

`UNITARES_MODEL_PRIVACY=local|external` overrides the classification for an
operator whose own server sits on a public address. A `privacy='local'` request
against an `external` endpoint is refused with a named error. It is never sent.

An `external` endpoint that is given a key must use `https`. An `http` external
URL with a key is refused before any request, with a named error, unless the
operator sets `UNITARES_MODEL_ALLOW_INSECURE_HTTP=1`; otherwise the key would
cross the network in plaintext on every call. A `local` endpoint may use `http`
(Ollama on loopback does).

### 2.4 The cloud fallback is a second endpoint, not a provider

`consult(privacy='cloud_allowed')` and `call_model(privacy='auto'|'cloud')` fall
back to the Hugging Face router today. That becomes an optional second
endpoint with the same three settings under `UNITARES_MODEL_FALLBACK_*`. When
those are unset and `HF_TOKEN` is set, the fallback is the Hugging Face router
as today, so behavior does not change. This is one primary and one fallback,
not a router: nothing chooses between providers by cost or load.

### 2.5 Discovery and diagnosis

- The `ollama:local` registry record keeps its `host_id` (it is part of the
  `call_model` contract) but reports the configured endpoint's kind, model and
  privacy class. Availability becomes `GET {base}/models`, which every
  OpenAI-compatible server answers, instead of a socket probe on the Ollama
  port. The existing 0.5 s budget suits a `local` endpoint only; an `external`
  one also pays for DNS, TLS and the model list, so it gets 3 s. Both are
  overridable with `UNITARES_MODEL_PROBE_TIMEOUT_S`. The 5 s cache and the
  rule that the probe runs off the event loop stay.
- The doctor gains a check that the endpoint answers and lists the configured
  model.
- `unitares model` (`cmd_model` in `scripts/unitares`, backed by
  `scripts/install/choose_model.py`, which today reads Ollama's `/api/tags`
  and writes only the Ollama names) lists models from `{base}/models`, so it
  works for any such server (step 1). Ollama instructions become one example
  in the manual.

### 2.6 The agent processes follow, without carrying the key

The reviewer's `local` backend and the local resident runner use
`local_model_client.py` and the same settings.

The orchestrated reviewer is started through a governed spawn whose
environment becomes an audited effect record, so
`orchestrator_dispatch.py` forwards configuration but never credential
values. The same rule applies here, and while the old names are in the alias
table (2.1.1) each is forwarded beside its new name. The dispatcher forwards
`UNITARES_MODEL_BASE_URL`, `UNITARES_MODEL` and `UNITARES_MODEL_API_KEY_ENV`
(the variable's name), and the key's value must be provisioned in the
orchestrator service's own environment, which the child inherits. With the
value only in the governance server's environment, server calls succeed and
the reviewer's call fails authentication and falls back, which its provenance
already records. The manual says this where it documents the key.

The reviewer's `external` backend stays, as the way to review with a different
model than the one that wrote the work, and inherits the primary endpoint's
settings when its own are unset.

## 3. Non-goals

- No router or model marketplace. One primary endpoint, one optional fallback.
- No metered default. The default remains a local Ollama address, per the
  execution-cost policy in `CLAUDE.md`. A metered endpoint is used only when an
  operator names it.
- No change to the host-adapter lane (`delegate_inference`,
  `consult(effort='thorough')`), which stays an operator extension.
- No change to `consult`'s contract. Its route postconditions change only so
  they accept the resolved primary and fallback routes (step 3).

## 4. Staging

Each step is its own pull request. Steps 1 to 3 preserve behavior for an
install that changes no setting; step 4 is the one deliberate change, and its
release note says what to set first. One rule orders them: no step may let a request with
`privacy='local'` reach an endpoint the server has not classified as local.

1. **Settings, with the privacy check.** In one pull request:
   - `UNITARES_MODEL_BASE_URL` and `UNITARES_MODEL` in `local_inference_env.py`,
     with the alias table, its expiry test and the doctor INFO line (2.1.1);
   - the endpoint classification and the `privacy='local'` refusal from 2.3,
     applied to every path that reads the setting, because this is the first
     step in which the base URL can name a machine the operator does not run;
   - the `docker-compose.yml` mappings for `governance-mcp`, beside the
     existing `UNITARES_OLLAMA_BASE`, `UNITARES_OLLAMA_BASE_URL` and
     `UNITARES_LLM_MODEL` lines: the two endpoint settings and the classifier's
     `UNITARES_MODEL_LOCAL_HOSTS` and `UNITARES_MODEL_PRIVACY`, or `.env`
     values never reach the server (a model reached by a Compose service name
     such as `vllm` is `local` only when that name is listed);
   - `unitares model`: `scripts/install/choose_model.py` lists models from
     `{base}/models` instead of Ollama's `/api/tags`, writes the new names, and
     keeps its Ollama-only steps (pull hints, the `host.docker.internal`
     rewrite) behind Ollama detection, with its tests;
   - Ollama detection itself (one cached `GET {root}/api/version` with the
     local probe budget), and the in-process reviewer's native `/api/chat`
     attempt made only when it succeeds. Without this, a non-Ollama endpoint
     set in this step would make every structured review wait out the native
     route's 60 s timeout before falling back;
   - the macOS LaunchAgent template
     (`scripts/ops/com.unitares.governance-mcp.plist`, which today carries
     only `UNITARES_LLM_MODEL`) and its install instructions, with the two
     endpoint settings and the two classifier settings;
   - the doctor check, and the manual and `.env.example` text.

   No client changes and no key setting yet: every client still sends a fixed
   key, so documenting one here would describe a setting nothing reads.
2. **Client and key.** Add `local_model_client.py` and
   `UNITARES_MODEL_API_KEY_ENV` together, with the `https` rule for credentialed
   external endpoints and its `UNITARES_MODEL_ALLOW_INSECURE_HTTP` opt-in (2.3).
   Every request to the endpoint sends
   the key, including the model-listing requests of `unitares model`, the
   doctor check and (from step 3) the registry probe, or an authenticated
   endpoint answers inference but reads as down during setup and diagnosis.
   This step's settings join the list in 2.1.2, so Compose and the LaunchAgent
   template carry `UNITARES_MODEL_API_KEY_ENV`,
   `UNITARES_MODEL_ALLOW_INSECURE_HTTP` and the key value under its default
   name. For the key value that default name is the only one Compose carries:
   Compose forwards variables it names, not ones chosen at run time, so a
   Compose install keeps its key in `UNITARES_MODEL_API_KEY`, and the manual
   says so. A custom `UNITARES_MODEL_API_KEY_ENV` is for source installs and
   for the orchestrator's own environment. Move
   all six constructions onto the client (the four in the server, the
   reviewer's `local` backend and the local resident runner), forward the key's
   name to the orchestrated reviewer as 2.6 describes, and keep `/api/chat` behind the step-1 Ollama
   detection. Only now does the manual describe authenticated
   endpoints.
3. **Discovery and fallback.** The `/models` availability probe for the
   registry, sending the key as step 2 does; the registry record for
   `ollama:local` reporting the configured endpoint's kind, and in the same
   change `call_model`'s host dispatch (`model_inference.py`, which today
   accepts only the `ollama` and `hf` provider kinds and forces
   `privacy='local'` on the Ollama branch) routing that host id to the primary
   endpoint whatever its kind, with privacy taken from the classification; and
   the fallback endpoint with the Hugging Face default. Consult's route
   postconditions (`_delivery_postcondition_error` in `consultation.py`, which
   accepts only fixed Ollama and Hugging Face route tuples) change in the same
   step to accept the resolved primary and fallback routes and to check the
   privacy class the classification produced, or a successful call to a
   non-Ollama endpoint is reported as a postcondition failure.
4. **No implicit model.** Remove the `gemma4:latest` fallback (decision 7.2),
   one release after step 1, whose doctor check warns when no model is named.
   After it, an install that names no model has consult and the local reviewer
   off, as the install manual already describes. Removing the fallback alone
   does not do that, so the same step adds an explicit not-configured state:
   the registry reports the local host `configured: false` with the reason; the
   shared client refuses before any request with a named
   `MODEL_NOT_CONFIGURED` error; the in-process reviewer abstains with that
   reason instead of calling; the dispatcher does not spawn an orchestrated
   reviewer whose backend is `local` without a model; and the reviewer and
   resident processes check for a model at start and report the same reason. The release notes say that
   deployments which relied on the implicit model must set `UNITARES_MODEL`
   (or the older `UNITARES_LLM_MODEL`) first, and that includes the original
   operator's deployment, which names none today. Agent processes that read
   the same resolver (the orchestrated reviewer's `local` backend, the local
   resident runner) follow the same rule.
5. **Contract.** Accept `cloud_allowed` in `call_model`'s `privacy` and describe
   `provider` as primary or fallback. This moves input-schema digests, so it
   waits for the next batched interface-contract release (see
   `interface-contract-release-batching-v0.md`, PR #2515, if accepted).

## 5. Evidence required before each step ships

### 5.1 Structured output

Step 2 may drop `/api/chat` for Ollama only after the reviewer's structured
answers through `response_format` match or beat the native route on the same
real review prompts, scored the way the current reviewer's outputs are scored.
The existing code notes a failure this must not reintroduce: constrained
decoding with thinking disabled returned schema-valid objects with every field
empty on a qwen model (`llm_delegation.py`, `call_local_llm_structured`).

### 5.2 Other servers

Before the manual says "any OpenAI-compatible server", steps 1 to 3 must pass
against at least Ollama and one non-Ollama server (vLLM or llama.cpp's server)
in CI or a recorded manual run.

## 6. Risks

- **Model-name heuristics.** `model_inference.py` estimates energy cost from the
  model family (llama, qwen, gemma). An unrecognized model id needs a neutral
  default, not an error.
- **Private-address edge cases.** Docker bridges, Tailscale's 100.64.0.0/10 and
  split-horizon DNS can make a local server look external or the reverse. The
  override exists for that; the default list is a decision (7.3).
- **Timeouts.** `llm_assisted_dialectic`, which `dialectic(reviewer_mode='llm')`
  reaches, has a 45 s tool timeout while one warm gemma4 call measures 43 to
  70 s (`model_inference.py`). That mismatch
  predates this proposal and is not fixed by it; step 2 should not make it worse.

## 7. Decisions (operator, 2026-09-27)

1. **Names.** The operator left this to the proposal. Decided:
   `UNITARES_MODEL_BASE_URL`, `UNITARES_MODEL` and `UNITARES_MODEL_API_KEY_ENV`
   become the documented names, and `UNITARES_OLLAMA_BASE`,
   `UNITARES_OLLAMA_BASE_URL` and `UNITARES_LLM_MODEL` stay as aliases under
   the expiry rule in 2.1.1. A name containing `OLLAMA` tells an installer with
   another server that the setting is not for them. Asked whether aliases would
   build up, the operator chose "best for federation": expiring aliases rather
   than a hard rename, because operators and the processes on one host upgrade
   independently.
2. **Default model: none.** "We don't know what outside users use." The
   implicit `gemma4:latest` goes, in its own step (section 4, step 4) after a release
   with a doctor warning, so deployments that relied on it can name a model
   first.
3. **Local addresses.** Tailscale is not part of UNITARES setup. The endpoint
   check reuses the server's existing trusted-network list, which already
   includes 100.64.0.0/10, rather than defining "local" a second way (2.3).
