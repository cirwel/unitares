# Onboarding policy v0: the server decides whether harnesses may bind identities

**Status:** Draft (2026-11-21). The server half (§7 step 1) is implemented in #3361; the rest is design only. It answers the operator's direction of 2026-11-21: every agent should be onboarded, across vendors, runtimes and harnesses, and hitching on should be easy and should not assume one workflow. It reverses the plugin's default of 2026-10-03 (`unitares-governance-plugin` #184, auto-onboarding off). Merging this document authorises nothing. Every step in §7 that changes a default or the identity path is operator-reserved.

## 1. Problem

An identity is something an agent claims by calling `start_session()`. An adapter can also bind one for a session that never did. That bind is labelled as made by the harness (`onboard_origin` is `harness_backstop` or `orchestrated_resume`), not as the agent's claim. A session with no identity still sends identity-free floor observations.

Whether an adapter binds is decided per client today, and differently per host:

| Host | Default | Where it is set |
|---|---|---|
| Claude Code and Codex plugin | off | `UNITARES_AUTO_ONBOARD`, `config/defaults.env` in the plugin |
| Hermes binding | on | onboards at the first turn of each session |
| Antigravity hooks | whatever the hook command sets | the reference deployment sets `on` inline |
| Bridge launcher (`thread-anchor-exec`) | off; `--bind` opts in | sets `UNITARES_AUTO_ONBOARD=anchored` |

The plugin default went to off on 2026-10-03 so that "a fresh install never mints identities on the target server unasked". The cost is that an adopter who installs the plugin and points it at their own server sees only floor observations, unless their agent happens to call `start_session()` or they find the variable. The reference deployment overrides the default to `on`, so the default path is not the one its own operator runs.

The stated reason is also weaker than it reads. The plugin's default target is `http://localhost:8767`. An identity is created there only if a UNITARES server is listening, and the person who started that server is the one whose database is written.

## 2. Goal

- An agent on any supported host gets an identity without its operator learning a per-host setting.
- One place decides whether harness-made binds are accepted: the server, whose operator owns the record.
- A client can still refuse to bind from its own machine.
- Nothing is sent to an endpoint the client has no reason to trust.

Not goals: changing what an agent-authored `start_session()` does, changing lineage rules, or making a harness-made bind count as an agent's claim.

## 3. Design

### 3.1 The server enforces a policy on the bind itself

A server setting, `UNITARES_HARNESS_ONBOARDING`, takes `welcome` or `agent_only`.

The onboard handler applies it to a call that asks. An adapter marks its identity-creating call `harness_request=true`. Under `agent_only`, such a call creates nothing when it would have created an identity, and returns a typed refusal, `harness_onboarding_declined`. A call that resumes an existing identity is not declined: it creates nothing, and declining it would cost a conversation the identity its agent claimed. An agent-authored call is unaffected. Under `welcome` every call proceeds as today.

Enforcement is on the write path because a published hint binds nobody. A client set explicitly to `on`, an older adapter, or a stale cache would all ignore it.

The policy is not keyed on `onboard_origin`. That label is observability only and by its own contract must never influence policy (`src/identity/onboard_provenance.py`). `harness_request` is a separate flag whose only meaning is "hold this call to the operator's policy". It is still declared by the caller: a client that omits it is treated as an agent, as now. The policy is a control for adapters that ask, not a security boundary. §6 says so again.

### 3.2 The default depends on exposure

- Bound to loopback (the default listen address): `welcome`.
- Bound beyond loopback (`UNITARES_BIND_ALL_INTERFACES=1` or a non-loopback `UNITARES_MCP_HOST`): `agent_only`, with a startup log line naming the setting. The operator opts in once.

A reverse proxy in front of a loopback server is exposed in fact while bound to loopback. The server cannot see that. The documentation for tunnels and proxies has to say to set the policy deliberately.

Whether auth is enabled does not set the default. Auth says who may call; it does not say the operator wants every caller's session registered.

### 3.3 Adapters default to asking

`UNITARES_AUTO_ONBOARD` gains `auto` and it becomes the default in the plugin. Under `auto` the adapter attempts the bind at the point it would under `on`, marked `harness_request=true`, and a `harness_onboarding_declined` answer leaves the session on the identity-free floor without a warning.

Explicit values keep their meaning:

| Client value | Behaviour |
|---|---|
| `off`, or `UNITARES_DISABLE_AUTO_ONBOARD=1` | never attempts a bind |
| `anchored` | attempts only for the owner of a thread anchor |
| `on` | attempts for every session |
| `auto` (new default) | attempts when §3.4 allows the endpoint |

No client value overrides a server that declines. That is a change: today `on` always binds.

There is no cache of the policy between the session-start hook and the Stop hook. The Stop hook asks by attempting, so a policy changed mid-session takes effect at the next attempt, and a session whose start hook did not run behaves the same as any other.

### 3.4 The client decides whether it trusts the endpoint

Attempting a bind sends the agent's name, model type, workspace basename and host session id. A floor observation sends less. Under `auto` the adapter attempts only when:

- the server URL is loopback, or
- the server URL was set explicitly by the operator and is HTTPS, or the operator set `UNITARES_AUTO_ONBOARD=on`.

and the endpoint's `/health` answer identifies it as a UNITARES server. A plain-HTTP remote URL under `auto` does not get a bind attempt; the session-start context says why.

`/health` also publishes the policy, under `onboarding.harness`, so the session-start context can tell the agent what will happen. It is a description. Nothing depends on it for enforcement.

### 3.5 Older servers and Hermes

A server that predates this ignores the policy and accepts every harness bind, as it does now. So:

- A plugin on `auto` against an older loopback server starts binding. That is the intended outcome for an adopter and a behaviour change for anyone who relied on the old default; the release note must say it.
- Hermes keeps onboarding by default. It adopts `auto` semantics only in the sense that it accepts a decline.

No adapter treats a missing policy as a refusal. That would turn Hermes off against every server not yet upgraded.

### 3.6 Nested processes

A CLI that an agent starts inherits its environment. Under `welcome` and `auto` it gets an identity of its own, with no link to the conversation that started it: a harness-made bind never declares a parent, and only an agent's own call declares lineage (`docs/ontology/identity.md`).

The reference deployment already holds about 1,200 identities with `onboard_origin=harness_backstop`. Binding every helper process by default adds to that, and an identity created without its context cannot be given one later.

So this ships with a descriptive field, not after it. When the plugin strips an inherited thread anchor from a process that does not own it (`hooks/run-hook.cmd`), it keeps the anchor as `spawned_under` and the harness bind declares it. The server stores it on the new identity as context, in the same class as `host_session`: it is not lineage, grants nothing, and is never promoted to `parent_agent_id`. A reader can then group or filter helper identities by the conversation they ran under.

Operator views need a filter on `onboard_origin` for the same reason.

## 4. What changes for whom

| Who | Today | After |
|---|---|---|
| New adopter, local server | floor observations only | every session has an identity |
| Operator of an exposed server | accepts any harness bind | declines them until set to `welcome` |
| Client with `on` against `agent_only` | binds | declined |
| Client with `off` | no bind | no bind |
| Hermes | binds | binds unless the server declines |
| Reference deployment | `on`, local server | unchanged in effect; nested CLIs gain `spawned_under` |

## 5. Consultation

One advisory consult on 2026-11-21 (Codex and Antigravity host adapters) reviewed an earlier version in which adapters read the policy from `/health` and cached it for the Stop hook, the default was `welcome` everywhere, a missing policy meant `agent_only`, and §3.6 was deferred. Both members advised against building that version. What changed in response:

- Enforcement moved from a `/health` hint to the onboard handler (both).
- The session-start to Stop cache was dropped (Antigravity; Codex asked for a bounded refresh instead).
- The default became conditional on exposure (both).
- A missing policy no longer means `agent_only`, which would have turned Hermes off (both).
- Endpoint trust was separated from server policy (both).
- `spawned_under` ships with the change (Antigravity; Codex would have deferred it).

Not adopted: conditioning the default on auth being enabled (§3.2), and a short-lived cache with refresh, which the attempt-and-accept-a-decline design makes unnecessary.

The consult is advice. It is not a review of an implementation.

## 6. Limits

- The policy relies on the adapter sending `harness_request`. It holds an adapter that asks, not a hostile client.
- A proxied or tunnelled loopback server gets the loopback default.
- Under `agent_only` the server still receives the onboard attempt before declining it.
- `spawned_under` records where a process ran, by the plugin's reading of an inherited variable. It is an observation.
- Nothing here addresses how long a thread anchor resolves. Whether a conversation idle past the session lifetime keeps its identity is a separate open question (KG `2026-11-21T11:13:36.826858+00:00`).
- Identity counts in reports will rise. Reports that count identities need the `onboard_origin` filter before this is turned on anywhere they matter.

## 7. Steps, each operator-reserved

1. Server: the setting, the handler refusal, the exposure default, `onboarding.harness` on `/health`, the `spawned_under` field, an interface contract bump. Touches the identity path, so two-family review.
2. Plugin: `auto` as the default, endpoint trust, accepting a decline, declaring `spawned_under`. Deploy after 1.
3. Host adapter (Hermes): accept a decline.
4. Operator views: filter on `onboard_origin`.
5. Documentation: the quickstart, the configuration reference, the tunnel and proxy guidance, release notes naming the default change.

## 8. Open decisions for the operator

- Whether `on` should stop overriding a server that declines (§3.3). This document says it should.
- Whether an exposed server should default to `agent_only` (§3.2), which makes the reference deployment's tunnel setups an explicit opt-in.
- Whether `spawned_under` is acceptable as context under the identity ontology, or reads too close to inferred lineage.
