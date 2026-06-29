---
name: beam-process-harness
description: >
  Use when spawning, owning, supervising, or driving an external OS process
  (especially a long-lived `claude` agent) from BEAM/OTP — the "born-in-BEAM"
  agent harness, the agent_orchestrator runner, or anything on the fleet that
  Port-owns a subprocess. Captures the VERIFIED stack tradeoffs (erlexec vs
  Port.open vs MuonTrap, pty-vs-pipe for NDJSON, OTP 28 priority messages,
  DynamicSupervisor restart semantics) and flags the one layer still
  un-de-risked (the claude stream-json control protocol). Fleet-specific
  (macOS / Apple Silicon, OTP 28, Elixir 1.19.5) — re-verify before relying.
last_verified: "2026-06-28"
freshness_days: 21
source_files:
  - unitares/elixir/agent_orchestrator/lib/agent_orchestrator/agent_runner.ex
  - unitares/elixir/agent_orchestrator/lib/agent_orchestrator/http_router.ex
---

# Driving External OS Processes from BEAM (the agent harness stack)

## Orientation

This is the reference for **owning a long-lived external OS process from BEAM**
— the canonical case being a persistent `claude` agent that the substrate feeds
on stdin and reads governance/output events from on stdout, under OTP
supervision (the "born-in-BEAM" agent harness). It also covers any fleet code
that Port-owns a subprocess (hardware/Lumen spawns, dashboard subprocess work,
future harness iterations).

**Fleet context this was verified against:** macOS / Apple Silicon, Erlang/OTP
**28**, Elixir **1.19.5**. The existing chassis is `agent_orchestrator`
(`AgentRunner` is **spawn-only** — `Port.open({:spawn_executable,...})`, owns
`os_pid`; `POST /v1/agents` spawns, no adopt-existing route). dispatch_beam uses
the same plain-Port + `claude -p`/stream-json shape.

**Why this skill exists:** the tradeoffs below cost a full deep-research arc
(fan-out + 3-vote adversarial verification) to pin down. Don't re-research them
— read this, then re-verify only the volatile/un-de-risked parts flagged below.

### Confidence tiers (read these markers)

- **[VERIFIED]** — survived 3-vote adversarial verification against a primary
  source. Trust it; re-check only if past `freshness_days`.
- **[DIRECTIONAL]** — standard/official but not adversarially confirmed here.
  Very likely right; confirm with a doc link before betting the design on it.
- **[HYPOTHESIS]** — sourced but UNVERIFIED (verification was starved by API
  rate-limits, came back abstain-not-refute). Treat as a lead, **not fact** —
  run the empirical gate before relying.

---

## 1. Owning the OS process: erlexec, not raw Port, not MuonTrap

**[VERIFIED] Use `erlexec` (saleyn/erlexec) to own a long-lived child.** It is
actively maintained (v2.3.4, 2026-06-12). **Pin a commit dated after
2026-06-23** — a *critical security/race-condition fix* landed that day. It
gives, over raw `Port.open`:

- **OS-child cleanup at port-program termination AND emulator exit** — the
  reason it's recommended over `Port.open` for "no orphans on BEAM crash." This
  is the fix for the `:brutal_kill`-skips-`terminate/2` → orphaned `agent:/<id>`
  lease class seen in the orchestrator.
- **Signals** (`:exec.kill/2`, `:exec.stop/1`) and `kill_group`.
- **stdin streaming** into a live child (`:exec.send/2`, `eof`).
- Elixir-native API (`:exec.start`, `:exec.run`).

  *Cleanup caveats (still your problem):* double-forked grandchildren that
  escape the process group survive; an emulator **SIGKILL** still orphans
  children (fundamental Unix limit, nothing fixes it).

**[VERIFIED] On macOS, `MuonTrap` is the WRONG tool here.** Its headline
"no-escapees / every descendant dies with the owner" guarantee is **Linux
cgroup-v2 only**; on darwin it degrades to SIGTERM/SIGKILL of the **direct child
only** (no tree teardown — its own test suite skips cgroup tests off-Linux). It
also explicitly disclaims interactive-stdin / signal use. ⇒ On this fleet,
erlexec is the only real process-tree-cleanup option. (MuonTrap is fine for a
*contained, fire-and-forget Linux daemon* — not our case.)

**[VERIFIED — go/no-go GATE] erlexec's Apple-Silicon support is UNDOCUMENTED.**
The README lists "MacOS X" but is **silent on arm64** — macOS-listed is not
arm64-confirmed. **Before depending on it, run a local smoke test:** build under
OTP 28 / Elixir 1.19.5, then spawn → stream stdin → send a signal → confirm
clean child teardown. Pass = GO; this is the cheapest way to retire the unknown.

| Axis | `Port.open` | **erlexec** | MuonTrap (darwin) |
|---|---|---|---|
| stream stdin to child | yes | **yes** (`:exec.send`) | disclaimed |
| send signals | no (close only) | **yes** (`:exec.kill`) | direct child only |
| pty | no | yes¹ | no |
| child-TREE cleanup on crash | no | **yes** (`kill_group`, emulator-exit) | **no** (cgroup=Linux) |

¹ pty exists but is a **NO-GO for the NDJSON channel** — see §2.

---

## 2. NDJSON channel: plain pipes, NEVER a pty

**[VERIFIED] Do not run the stream-json/NDJSON channel over a pty.** In pty mode
stdout and stderr are multiplexed onto a **single master fd** (one PTY slave
backs both fd 1 and fd 2 — POSIX-universal; erlexec README "PTY stdout/stderr
Separation" + issue #41). stderr lines then interleave into stdout and
**corrupt the newline-delimited JSON framing**. Use a **plain pipe** with
separated stdout/stderr.

**Consequence for "interactive" agents:** a clean programmatic NDJSON channel
and a human-interactive TTY view are **mutually exclusive on one process**. You
cannot get both off one `claude` child. The born-in-BEAM streaming agent needs
**no pty at all** (pipes + stream-json suffice); a genuinely interactive TTY is
a *separate, harder track*, not a flag on the same runner. (Do not repeat the
"interactive = just `pty: true`" mistake — it was falsified.)

---

## 3. Priority mailbox: OTP 28 priority messages, hand-rolled into the GenServer

**[VERIFIED] OTP 28 priority messages are production (EEP-76, not experimental).**
A receiver makes `PrioAlias = alias([priority])` and senders use
`erlang:send(PrioAlias, Msg, [priority])`; such messages are inserted **before
ordinary messages** (still in send-order), with no penalty on the ordinary lane.
Good fit for "urgent verdict (`pause`/`reject`) or dialectic invite jumps ahead
of routine leave-notes."

**[VERIFIED] BUT there is NO native `gen_server`/`GenServer` integration.** The
official docs scope priority messages to **raw `receive` + system signals
(exit/link/monitor) only**. Standard `handle_info` / `handle_call` ordering does
**not** honor the priority flag. So inside the runner GenServer you must
hand-roll it: a dedicated high-priority intake process, a custom receive
dispatch, or a third-party `pri_server`. Budget for this — it is not free.

---

## 4. Supervision & the liveness-coupling cost

**[DIRECTIONAL] Per-agent session actors → `DynamicSupervisor`** (starts empty,
`start_child/2` per agent; only `:one_for_one`). Use `restart: :transient` for a
runner that owns a `claude` child: restarts on crash, not on clean shutdown.

**Hold this cost of "born-in-BEAM" (model B):** agent liveness becomes coupled
to BEAM uptime.
- **GenServer restarts, VM stays up:** the external child can be made to survive
  (it's a separate OS process) — but you must re-attach, not re-spawn. erlexec
  can *manage/monitor an externally-started* os_pid (`exec:manage`), which is the
  hook for a re-attach/hand-off pattern. **[HYPOTHESIS — verify before relying.]**
- **Whole VM restarts/redeploys:** Port/erlexec ownership is lost; without a
  drain or re-adopt step, a harness redeploy **kills every in-flight agent.**
  Deploy = recompile-on-restart on this fleet (kickstart), so this is a real
  event, not theoretical. Design a drain / `claude --resume` re-attach story
  before generalizing the harness beyond ephemeral use.

---

## 5. The live agent channel (stream-json) — UN-DE-RISKED, verify empirically

**[HYPOTHESIS — the single biggest remaining build risk.]** The `claude` CLI
stream-json control protocol could **not** be confirmed by research (every claim
came back abstain-not-refute). The reverse-engineered picture below is a
**lead, not fact** — confirm it against the *installed* `claude` CLI before
building on it:

- Invocation: `claude --input-format stream-json --output-format stream-json --verbose`.
- `system`/`init` message at session start carries `session_id`.
- Bidirectional **control** messages via `control_request`/`control_response`
  matched by `request_id` (subtypes: initialize / permission / mcp_message).
- Inject an additional user turn mid-session by writing a stdin line:
  `{"type":"user","message":{"role":"user","content":...}}`.
- Session resume via `--resume <session_id>`.
- Whether bidirectional stdin streaming is *officially supported* vs
  reverse-engineered, the exact schema, interrupt/cancellation, and backpressure
  when another program consumes stdout — **all unconfirmed.**

**Empirical gate (run before any §5 design work):** drive the installed CLI with
the flags above, inject a mid-session user turn on stdin, capture the actual
message schema + control frames, and confirm protocol stability across the CLI
version you ship against. This produces ground truth the open web did not have.

---

## Freshness

- **Volatile — re-check every run:** the erlexec version/commit (5 releases in
  ~6 weeks; pin post-2026-06-23). The claude stream-json protocol (§5) is
  version-fluid and was never confirmed.
- **Stable — won't rot:** pty merges stderr (POSIX); priority-messages have no
  GenServer integration (OTP 28 design); MuonTrap tree-cleanup is Linux-only.

## Sources

- erlexec — https://github.com/saleyn/erlexec (+ issue #41 for pty)
- MuonTrap — https://github.com/fhunleth/muontrap
- OTP 28 priority messages — https://www.erlang.org/blog/highlights-otp-28/ ·
  EEP-76 https://www.erlang.org/eeps/eep-0076 · PR https://github.com/erlang/otp/pull/9269
- DynamicSupervisor / restart — https://hexdocs.pm/elixir/DynamicSupervisor.html
- claude stream-json (unverified) — reverse-engineered SDK protocol notes;
  confirm against the installed CLI, not these.
