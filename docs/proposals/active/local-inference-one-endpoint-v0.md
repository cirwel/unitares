# One local model endpoint (v0)

Status: Proposed, design-only. Nothing here is authorized to build until the
operator decides the three open questions in section 7.

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
local resident runner (`agents/local_resident/runner.py`). Its `external` backend
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
| `UNITARES_MODEL` | Model id the endpoint serves | `UNITARES_LLM_MODEL` if set, else `gemma4:latest` (see 7.2) |
| `UNITARES_MODEL_API_KEY_ENV` | Name of the variable that holds the endpoint's key, if it needs one | `UNITARES_MODEL_API_KEY` |

The key is named indirectly, as the orchestrated reviewer's `external` backend
already does (`UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV`), so a process that must
not carry the value (see 2.6) can be told which variable to read. Ollama,
vLLM, LM Studio, llama.cpp's server, OpenRouter, OpenAI and the Hugging Face
router all accept this shape. The existing names stay as aliases with the
precedence and disagreement warning `local_inference_env.py` already applies to
`UNITARES_OLLAMA_BASE_URL`. An existing install keeps working with no edits.

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
server classifies the configured endpoint:

- `local` when the host is loopback, an RFC 1918 or RFC 4193 private address,
  `host.docker.internal`, or a name listed in `UNITARES_MODEL_LOCAL_HOSTS`;
- `external` otherwise.

`UNITARES_MODEL_PRIVACY=local|external` overrides the classification for an
operator whose own server sits on a public address. A `privacy='local'` request
against an `external` endpoint is refused with a named error. It is never sent.

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
  privacy class. Availability becomes `GET {base}/models` with the existing
  0.5 s budget and 5 s cache, which every OpenAI-compatible server answers,
  instead of a socket probe on the Ollama port.
- The doctor gains a check that the endpoint answers and lists the configured
  model.
- `unitares model` (`cmd_model` in `scripts/unitares`, backed by
  `scripts/install/choose_model.py`) lists models from `{base}/models`, so it
  works for any such server. Ollama instructions become one example in the
  manual.

### 2.6 The agent processes follow, without carrying the key

The reviewer's `local` backend and the local resident runner use
`local_model_client.py` and the same settings.

The orchestrated reviewer is started through a governed spawn whose
environment becomes an audited effect record, so
`orchestrator_dispatch.py` forwards configuration but never credential
values. The same rule applies here: the dispatcher forwards
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
- No change to `consult`'s contract or its route postconditions.

## 4. Staging

Each step is its own pull request and preserves behavior until the last.

1. **Settings.** Add `UNITARES_MODEL_BASE_URL` and `UNITARES_MODEL` with their
   aliases in `local_inference_env.py`, the doctor check, and the manual and
   `.env.example` text. No client changes, and no key setting yet: every
   client still sends a fixed key, so documenting one here would describe a
   setting nothing reads.
2. **Client and key.** Add `local_model_client.py` and
   `UNITARES_MODEL_API_KEY_ENV` together. Move all six constructions onto the
   client (the four in the server, the reviewer's `local` backend and the local
   resident runner), forward the key's name to the orchestrated reviewer as
   2.6 describes, and keep `/api/chat` behind Ollama detection. Only now does
   the manual describe authenticated endpoints.
3. **Privacy and fallback.** Endpoint classification and the `privacy='local'`
   refusal, the `/models` availability probe, and the fallback endpoint with the
   Hugging Face default.
4. **Contract.** Accept `cloud_allowed` in `call_model`'s `privacy` and describe
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

## 7. Decisions for the operator

1. **Names.** Introduce `UNITARES_MODEL_*` as the documented names with the
   current ones as aliases (proposed), or keep `UNITARES_OLLAMA_BASE` canonical
   and add only an API-key setting.
2. **Default model.** Keep `gemma4:latest` when nothing is named (proposed,
   preserves behavior), or default to no model so consult and reviews stay off
   until the installer names one, which is what the manual already describes.
3. **Local address list.** Whether Tailscale's 100.64.0.0/10 counts as local by
   default.
