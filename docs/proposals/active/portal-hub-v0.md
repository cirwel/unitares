# Portal hub v0: start, resume and talk to any vendor's sessions from one app

**Status:** Draft (2026-10-26), design only. This document answers the operator's hub direction of 2026-10-26 (KG `2026-10-26T04:55:07.374033+00:00`). It builds on the portal direction of 2026-10-23 (KG `2026-10-23T11:39:51.328628+00:00`) and on [`portal-live-timeline-v0.md`](portal-live-timeline-v0.md), whose §6 M5 anticipated a rename to "the portal" once a third pane arrived. The hub is that third pane. Every step that writes is operator-reserved (§8). Merging this document authorises none of them.

## 1. Problem and goal

The operator works with several vendors' agents at once: Claude, Codex, Gemini and Antigravity. Each vendor signals well when its own agent needs him. The cost is moving between terminal windows and vendor apps to find the session, and having no single place to start or resume work. In the operator's words: "having one app where a user can message or start a new session would be helpful, a hub. For a user only using claude- they would stay with claude. but a user like me uses three frontier models with an addition of antigravity ... it would be helpful to centralize it." He then added "but resuming sessions too", and asked whether the hub should be a separate app.

The goal is one local app where a multi-vendor operator can:
- see every session (the live timeline, already built);
- resume a session that was started in a terminal;
- start a new session with a chosen vendor;
- keep talking to any session the hub runs.

A single-vendor user stays in that vendor's app, and nothing in the core requires the hub.

**Where it differs from what exists.** Cross-vendor launchers and GUIs already exist. CloudCLI, for example, is an open-source web UI over Claude Code, Codex, Cursor CLI and Gemini CLI. A launcher alone is therefore not this project's contribution. The hub's value is the record underneath it: one identity per session across vendors, declared lineage when a session is resumed, cross-family review, the timeline, and outcomes. A hub without UNITARES behind it would be one more launcher. The hub therefore requires UNITARES and does not try to stand alone.

**Earlier attempt.** Discord dispatch, through the BEAM Discord bridge, carried multi-turn conversations to `claude -p` children of the agent-orchestrator. It supplies the turn model in §3.4. The operator's account of why it did not become the daily surface (2026-10-26):
- It was mostly one-way.
- It was a little buggy.
- It was hard to make compete with the vendors' own apps.

Those apps resume sessions well, Claude's especially, so he keeps sessions in the vendor apps and toggles between them. Codex is the gap: a terminal session cannot be resumed from its phone app, only a cloud session. The hub therefore has to be two-way and reliable from its first build. It competes only where no vendor app can: across vendors, and on the record.

## 2. What exists

- **Timeline.** The live timeline at `http://localhost:8790/timeline` (`elixir/dialectic_live`, `TimelineLive`) shows one row per agent with its latest note, a "needs your attention" box, a background group and a live light. It is read-only.
- **Agent-orchestrator** (`elixir/agent_orchestrator`, `127.0.0.1:8789`, bearer-guarded). It spawns supervised OS processes through `POST /v1/agents`, collects their output, and offers idempotent spawns (`SpawnGate`). It gives children a stable governance anchor (`client_session_id` with `UNITARES_ORCHESTRATED=1`), so the headless turns of one conversation stay one governance identity (`agent_runner.ex`, "Session-anchor provisioning").
- **Host adapters.** Codex, Claude and Antigravity run through the orchestrator today for `consult` and `delegate_inference` (`list_inference_hosts`).
- **Launch profiles (not built).** Named launch profiles were decided on 2026-10-15 (KG `2026-10-15T10:29:57.051370+00:00`, [`inference-hosts-config-v0.md`](inference-hosts-config-v0.md) §2.5) but are not built. The orchestrator still accepts caller-chosen `cmd`, `args`, `env` and `cd`. §2.5 shows that a caller with the bearer token can then run arbitrary code.
- **Vendor session continuity.** Claude Code and Codex keep sessions on disk and resume them by id (Claude `--resume`, Codex `resume`). Gemini CLI and Antigravity have not been checked.
- **What the record lacks.** The governance record does not know a session's vendor session id. The plugin names its local cache `.unitares/session-<host_session_id>.json`, but no hook sends that id to the server, so a timeline row cannot say which vendor session to resume.
- **Steering council, 2026-10-26** (KG `2026-10-26T04:13:08.316719+00:00`). Two outside model families agreed independently on three points:
  - Loopback plus Host and Origin checks do not stop a local process, an agent included, from driving a LiveView socket. Any write therefore needs a real secret.
  - A write should be recorded as the operator acting through the portal.
  - A write must fail closed if the state it was shown has changed.

## 3. Design

### 3.1 Placement: a separate app in this repository

The hub is its own OTP application, `elixir/portal`, with its own port, process, login and credentials. It is not more panes inside the governance server, and it is not a write path added to `dialectic_live`.

- **Separate from the server.** The server records; the hub starts programs, spends subscriptions and holds the operator's login. If the hub fails or is misused, the record must keep working, and an install without the hub loses nothing.
- **Separate from `dialectic_live`.** `dialectic_live` is deliberately read-only, and its boundary (portal-live-timeline §3.5) is sized for that. The timeline moves into the hub as its home screen, the "portal" rename that §6 M5 anticipated. `dialectic_live` keeps the dialectic pane until the operator decides its future.
- **Same repository for now.** It reuses CI, the deploy path (`cirwel update`), the release tooling and the orchestrator next door. It moves to its own repository when someone wants it without UNITARES.

### 3.2 Login and boundary (before any write)

The hub ships with these from its first build, not as a later hardening step:
- `127.0.0.1` bind, with a refusal to boot on any wider address;
- an explicit Host allowlist plug and explicit `check_origin`;
- one guarded `live_session` that fails closed without an authenticated session.

**Login: the dashboard's passkey, reused** (operator choice, 2026-10-26). The governance server already runs passkey (WebAuthn) sign-in with opaque, server-side dashboard sessions (`src/dashboard_auth.py`). That module tracks three things for a session: whether it is signed in, whether it may write, and whether it is fresh. The hub uses that sign-in instead of building a second one:
- the hub sends the operator through the server's passkey ceremony;
- the hub asks the server to validate the resulting session on every connect and every write;
- a write needs a fresh session. Under the server's current rule (`dashboard_session_is_fresh`), that means one signed in within the last ten minutes. An older session therefore asks for the operator's fingerprint or face again, but several starts or resumes inside one ten-minute window share a single sign-in. If each spawn should carry its own biometric check, that needs a new action-bound ceremony, not the existing freshness check. H2 chooses between the two.

The hub never stores a credential of its own for the browser. Without a valid session, a page on the operator's machine, or a process that can reach the port, can see nothing and do nothing.

H2 must settle two details:
- whether the deployment's passkey relying-party id (`UNITARES_DASHBOARD_RP_ID`) covers the hub's origin;
- how the hub validates a session without reading the server's database. The likely answer is a small read-only session-check route, which is itself an auth-path change.

A one-time launch token exchanged for an `HttpOnly`, `SameSite=Strict` cookie remains the fallback for an install with passkeys off.

**Credentials.** The hub calls the orchestrator through named launch profiles only, never with caller-chosen `cmd` or `env`. It holds its own operator-class credential, issued per client as `identity/operator.py` advises. It never holds the whole secrets file.

**Attribution.** Every hub action records both the operator, as the authorising principal, and the hub, as the executing client, together with the action, its target, a request id and the outcome.

### 3.3 Resume (first feature)

A timeline row whose session can be resumed shows a Resume control. Resuming:
1. Re-reads the row's current state and fails closed if it has changed since the operator saw it.
2. Checks the one-driver rule: a session still open elsewhere is refused, or offered as a fork. Two drivers writing one transcript is the failure to prevent.
3. Spawns the vendor's resume through a named launch profile, for example Claude `--resume <id>` or Codex `resume <id>`, with the vendor session id taken from the record (§2).
4. Opens a conversation view (§3.4) on that row.

**Identity.** A resume runs in a new process. Under the identity contract it mints a fresh identity with `parent_agent_id` set to the original and `spawn_reason="explicit"`, and the timeline draws the two rows linked. Later turns inside the hub are orchestrated turn-children under one anchor, so they stay that one new identity.

**Coverage.** Resume covers vendors whose CLI resumes by id and whose hook reports the vendor session id. Sessions inside vendor GUI apps (the Claude desktop app, Cursor's editor) stay visible but cannot be resumed. The row says which case applies.

### 3.4 Conversation model

Each operator message is one headless turn: a fresh process that continues the vendor session, the shape Discord dispatch used. The hub streams the turn's output into the view and appends the reply to the row. A turn waiting on approval surfaces in the view rather than auto-approving. Whether the hub may ever answer approvals is out of scope for v0 (§4).

### 3.5 Start a new session (second feature)

The operator picks a vendor and a launch profile, types the task, and the hub spawns it with a fresh identity. It appears as a new timeline row the operator can keep talking to. A council or a cross-family review can be requested from the same view through the existing `consult` and review paths. The hub adds no review mechanism of its own.

## 4. Non-goals for v0

- Typing into a session that a vendor's own app or a terminal is still driving.
- Answering tool-permission prompts from the hub.
- Agent-to-agent messaging. This is agent-to-human; `/v1/msg` and the A2A variants stay separate.
- New governance tools or actions. Exposure beyond loopback, multi-user use, mobile or a hosted version.
- A pause or resume control over governance pauses. The 2026-10-26 council parked that until a pause actually needs the operator.

## 5. Milestones

Each milestone is a separate draft PR. **H0** is agent-column work (record data). **H1** onward write, so each is operator-reserved.

- **H0: the record learns the vendor session id.** The plugin's hooks report the vendor and its session id on onboard, and `/v1/timeline` returns them per agent. This is read-only data. It lets the existing timeline label each row resumable or not. It needs a check that the field carries no secret and stays bounded.
- **H1: launch profiles.** Build `inference-hosts-config-v0` step 4 (§2.5) in the orchestrator: named profiles for resume and start per vendor, with no caller-chosen `cmd` or `env`. This is a prerequisite for any hub spawn, and useful on its own because it closes the bearer-holder hole.
- **H2: the hub shell.** `elixir/portal` with the §3.2 boundary and login, the timeline moved in as the home screen (read-only), and boundary tests for a foreign Host, a foreign Origin, no cookie and an expired token. It is shipped by `cirwel update` and registered like the other BEAM apps.
- **H3: resume.** §3.3 and §3.4, Claude first (operator, 2026-10-26: "ideally all, but you have to start somewhere"), with the stale-state and one-driver tests and dual attribution. Codex follows, because its terminal sessions are the ones its own phone app cannot resume.
- **H4: start a new session.** §3.5, then further vendors as their CLIs and hooks allow.

## 6. Risks and limits

- **Partial coverage by construction.** The hub sees each vendor at the level its CLI and hooks allow. The UI names each row's level instead of implying completeness.
- **A local-process threat remains.** A process running as the operator can read the secrets file whatever the hub does. The login bounds other pages and sandboxed agents, not the operator's own user.
- **Spend.** Starting and resuming sessions spends subscriptions. The hub shows which vendor and profile each spawn uses, and starts nothing on its own.
- **Runway.** H0 and H1 are useful without the hub. If the hub stops after them, nothing is wasted.
- **Vendor drift.** CLI resume flags and hook payloads change. Profiles isolate that to one file per vendor.
- **Competing with the vendors' apps.** Discord dispatch lost partly on polish against apps the operator already used (§1). The hub will not out-polish a vendor's chat. It earns its place only if it is reliable for the cross-vendor jobs: seeing everything at once, resuming where a vendor app cannot, and the record.
- **The phone.** Much of the toggling happens on the phone, and v0 is loopback-only. Reaching the hub from a phone means exposure beyond loopback: a tunnel plus passkey sign-in, which works on phones. That is a later, separately reserved step, not part of v0 (§4).

## 7. Operator answers (2026-10-26)

1. **Discord dispatch.** "its ok sometimes", but mostly one-way, a little buggy and hard to make compete with the vendors' apps. Vendor apps resume well, Claude's especially. Codex terminal sessions cannot be resumed from its phone app, only cloud sessions, so the operator keeps sessions in the apps and toggles. This is folded into §1 and §6.
2. **Placement and name.** Yes: `elixir/portal` in this repository.
3. **First vendor for resume.** Claude ("ideally all, but you have to start somewhere"). This is folded into H3.
4. **Login.** The dashboard's biometric (passkey) login. This is folded into §3.2.
5. **`dialectic_live` after the move.** Undecided ("it still looks like prototype phase"). It stays as it is, and the question returns when H2 moves the timeline.

## 8. Authorization

Merging this document records the design and authorises **H0 only**: reporting and returning the vendor and vendor session id as read-only record data, with its own review.

H1 changes the orchestrator's spawn boundary. H2 adds a new app with a login. H3 and H4 add actions that start or continue agent sessions. Each of these needs the operator's explicit approval on its own draft PR. Nothing here changes governance pauses, adds a governance tool or action, or touches the Wave 3 gated scope.
