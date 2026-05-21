# S19 — Attestation Mechanism Proposal (v2)

**Status:** Reviewer gate satisfied for **mechanism selection** (council pass 2026-04-25). Implementation correctness is a separate gate that lives in the implementation row's tests + adversary regression suite.
**Companion to:** `docs/ontology/plan.md` row S19; `docs/ontology/identity.md` "Pattern — Substrate-Earned Identity"
**Reviewers:** `feature-dev:code-architect` (mechanism review); `feature-dev:code-reviewer` (adversary review). Both 2026-04-25.
**v1 → v2 changes:** scope narrowed to 3 residents (Watcher excluded), binary-path verification added to registry + runtime check, PID start-time mitigation made required not optional, operator pre-seed enrollment confirmed as default, launchctl backend pinned to subprocess (no PyObjC).

---

## What this doc is

S19 was opened with the operator-accepted preferred direction "B-strict — server-side substrate-claim verification." This doc is the gate (S19 resolved-when (f)): mechanism selection complete; implementation row may now open. **No code is written until v2 lands**; this v2 IS the landing artifact.

## v2 scope (narrowed)

In-scope targets — **three** launchd-managed substrate-anchored residents on the Mac:

- Vigil (`com.unitares.vigil`, 30-min cron)
- Sentinel (`com.unitares.sentinel`, continuous)
- Chronicler (`com.unitares.chronicler`, daily)

**Watcher is excluded from M3.** Per `identity.md` worked examples, Watcher's behavioral continuity is *"weak — bound to LLM call, not trajectory"* — structurally session-like under the layered taxonomy. Forcing Watcher into substrate attestation would repeat the category error S19 is trying to fix. Watcher continues under whatever S1-a/A′ converges on, or a later Watcher-specific row if its leakiness becomes operational pressure. The adversary-reviewer's recommendation was decisive on this point; the architect's M3-hook variant is not adopted.

Out of scope (unchanged from v1):

- **Lumen** — embodied agent on dedicated Pi hardware. Cross-host (Pi → Mac MCP) constrains mechanism choice; addressed separately if S19 generalizes.
- **Steward** — in-process within governance-mcp, doesn't traverse the transport boundary.
- **Session-like agents** — Claude Code tabs, Codex sessions, third-party CLIs (Hermes). Continue under S1-a/A′ unchanged.

## Constraints (from S19 row)

1. **Server-verifiable or non-exportable.** No "another copyable secret in a plist."
2. **UUID stability preserved** (Codex code citations: `agents/sdk/src/unitares_sdk/agent.py:279`, `src/trajectory_identity.py:660`, `src/mcp_handlers/updates/enrichments.py:727`, `src/mcp_handlers/identity/resolution.py:679`).
3. **Closes PATH 2.8 leak for substrate-anchored class only.**
4. **No new identity layer for non-substrate-anchored agents.**

## Adversary models (refined per Q3 council resolution)

| Model | Description | M3 (v2) |
|---|---|---|
| **A1 — Same-UID copying anchor (Hermes case)** | Process running as same OS user reads `~/.unitares/anchors/<resident>.json` and presents contents to MCP | ✓ defeated |
| **A2 (naive) — Copy + connect** | A1 plus: connects via UDS with kernel-attested peer PID belonging to a non-launchd-managed process | ✓ defeated |
| **A2 (escalated) — Copy + binary substitution + `launchctl kickstart`** | A1 plus: replaces the resident binary at the plist-referenced path (same-UID writable on this Mac), then `launchctl kickstart user/$(id -u)/com.unitares.sentinel` to spawn under correct label | **✓ defeated by `expected_executable_path` registry field** (v2 addition); **residual deployment risk** if binary path is same-UID writable AND attacker can replace binary and *then* ensure the path-match check passes (current state on this Mac — flagged as deployment-configuration weakness, not silent acceptance) |
| **A3 — Process impersonation (root-equivalent or memory injection)** | Code injection into the actual launchd-managed process | Out of threat model (root-equivalent attacker defeats every mechanism in the project) |
| **(e) — PID reuse race** | Resident exits → window → attacker process reuses PID → connects before launchd reaps | ✓ defeated by `pid_start_tvsec` field in cached `(uuid, pid, start_tvsec)` triple |

## Mechanism (M3, v2-refined)

**M3-v2 = UDS transport + SO_PEERCRED + server-side launchctl label match + `expected_executable_path` runtime match + `(uuid, pid, start_tvsec)` per-server-lifetime cache.**

### Transport: UDS additive to existing HTTP

Governance-mcp gains a Unix-domain socket listener at `/Users/cirwel/.unitares/governance.sock` alongside the existing HTTP-on-loopback at port 8767. UDS is for substrate-anchored residents only; HTTP stays for non-substrate-anchored clients. Q1 confirmed by code-architect: HTTP-only listener today (`src/mcp_server.py:217-223, 714, 738-963`); UDS is additive surface; `SessionSignals` extends with one `peer_pid: Optional[int]` field; the existing ASGI pipeline handles requests identically once `peer_pid` is populated.

Per the anyio-asyncio constraint (CLAUDE.md "Known Issue"), the verification call (launchctl + executable path + start-time) MUST be wrapped in `loop.run_in_executor` — the existing pattern at `src/agent_loop_detection.py:374` is the reference. Verification result is cached in the connection's contextvar for that connection's lifetime; not re-verified per tool call.

### Substrate-claim registry

New schema in `core`:

```sql
CREATE TABLE core.substrate_claims (
    agent_uuid              UUID PRIMARY KEY,
    expected_launchd_label  TEXT NOT NULL,
    expected_executable_path TEXT NOT NULL,
    enrolled_at             TIMESTAMPTZ NOT NULL,
    enrolled_by_operator    BOOLEAN NOT NULL DEFAULT TRUE,
    notes                   TEXT
);
```

`expected_executable_path` is the v2 addition. Enrollment writes the path the operator declares (from the plist's `ProgramArguments[0]` or equivalent). At runtime, the server reads the connecting peer's actual executable path via `proc_pidpath(peer_pid)` (macOS) or `/proc/<peer_pid>/exe` (Linux) and compares.

### Enrollment workflow — operator pre-seed (no TOFU)

Adversary-reviewer §6 made this binding: TOFU lets the attacker who beats the resident to first connect own the substrate-claim permanently. The race window (governance-mcp accepting connections before residents have onboarded) is real on a development Mac with concurrent agent dispatch.

Operator pre-seed CLI: `scripts/ops/enroll-resident.sh <agent_uuid> <launchd_label> <executable_path>`. Run once per resident at deployment time, then on any registry rebuild. Inserts the `core.substrate_claims` row before the resident first connects.

**Loud warning (Codex's option (c)):** The CLI inspects the supplied `executable_path` and the parent directory chain. If any directory in the chain is same-UID-writable (i.e., not under `/usr/local/`, `/opt/homebrew/`, `/System/`, `/Library/`, or another root-owned path), the CLI emits a prominent warning to stderr and writes a deployment-risk audit event. Enrollment proceeds — the warning is informational, not blocking — but the event is durable and can be queried later.

The deployment-risk policy is documented in the proposal but not silently accepted: **M3 attests launchd identity and process instance, not binary immutability unless deployment hardening is done.** Operators who care about A2-escalated defeat move binaries to non-user-writable paths and re-enroll. Operators who don't get a stronger system than today, with the residual risk visible.

### Verification at connection-accept

For each new UDS connection from a substrate-anchored resident:

1. Read `peer_pid` via `LOCAL_PEERCRED` (macOS) or `SO_PEERCRED` (Linux).
2. Resolve the connecting `agent_uuid` from the onboard call's claim.
3. Look up `core.substrate_claims[agent_uuid]` → `(expected_launchd_label, expected_executable_path)`. If absent, reject (no enrollment).
4. Read peer's actual `launchd_label` via `peer_attestation.read_service_label(peer_pid)`. If mismatch with `expected_launchd_label`, reject.
5. Read peer's actual `executable_path` via `peer_attestation.read_executable_path(peer_pid)`. If mismatch with `expected_executable_path`, reject.
6. Read peer's `process_start_time` via `peer_attestation.read_process_start_time(peer_pid)` (macOS: `proc_pidinfo(PROC_PIDTBSDINFO).pbi_start_tvsec`; Linux: `/proc/<pid>/stat` field 22).
7. Check the in-process per-server-lifetime cache `verified_pairs[agent_uuid]`:
   - If absent: store `(peer_pid, start_tvsec)` — first verified connect.
   - If present and `(peer_pid, start_tvsec)` matches: accept.
   - If present and PID matches but `start_tvsec` differs: PID was reused; reject (process_start_time can't be reused for a fresh process).
   - If present and PID differs: process restart — accept and update cache (legitimate restart with new PID).

Step 6+7 closes Q3(e) per adversary-reviewer §3.

### `peer_attestation.py` module

New module at `src/substrate/peer_attestation.py`. Pure-Python interface, platform-selecting backends:

```python
def read_peer_pid(sock) -> int | None
def read_service_label(pid: int) -> str | None
def read_executable_path(pid: int) -> str | None
def read_process_start_time(pid: int) -> int | None  # tv_sec
```

**macOS backend** (initial implementation):
- `read_peer_pid`: `getsockopt(LOCAL_PEERCRED)` via stdlib `socket`.
- `read_service_label`: subprocess `launchctl print pid/<N>` + `re.search(r"^\s*label\s*=\s*(\S+)\s*$", output, re.M)`. Wrapped in `try/except` returning `None` on parse failure (treated as substrate-claim rejection per architect §Q2). Codex's pick: prefer `launchctl procinfo <pid>` if available (macOS 13+, machine-readable) and fall back to `launchctl print` parser. Both run via subprocess; **no PyObjC dependency**.
- `read_executable_path`: `proc_pidpath(pid, ...)` via `ctypes` against `libproc.dylib`. ~5ms, stable since macOS 10.5.
- `read_process_start_time`: `proc_pidinfo(pid, PROC_PIDTBSDINFO, ...)` via `ctypes`. Returns `pbi_start_tvsec` from BSD task info. Stable across all supported macOS versions.

**Linux backend** (deferred; module raises `NotImplementedError("S19 Linux backend not yet implemented")` if `sys.platform == "linux"`):
- `read_peer_pid`: `getsockopt(SO_PEERCRED)` returning `struct ucred`.
- `read_service_label`: parse `/proc/<pid>/cgroup` for `name=systemd:/system.slice/<unit>.service` form.
- `read_executable_path`: `os.readlink(f"/proc/{pid}/exe")`.
- `read_process_start_time`: `/proc/<pid>/stat` field 22.

Implementation tests use fixture outputs (recorded `launchctl print` and `launchctl procinfo` samples from the live Mac); narrow regex targeting; format-version detection that degrades cleanly to the older parser when newer fields are absent.

### SDK changes

`agents/sdk/src/unitares_sdk/agent.py:_ensure_identity` (line 279) and `_save_session` (per-resident) adopt:

- New env var `UNITARES_UDS_SOCKET=/Users/cirwel/.unitares/governance.sock` set in each substrate-anchored resident's plist. SDK's `GovernanceClient.connect()` (`agents/sdk/src/unitares_sdk/client.py:75-99`) detects this env var and routes through UDS instead of HTTP.
- Substrate-anchored mode detected by env var presence; in this mode, `_save_session()` writes only `{agent_uuid}` (and optional `parent_agent_id`); it does NOT persist `continuity_token` or `client_session_id`. The Steward-shape anchor becomes the canonical pattern for the substrate-anchored class.
- `_ensure_identity` fast-path: when `agent_uuid` is in the anchor and `UNITARES_UDS_SOCKET` is set, calls `client.identity(agent_uuid=agent_uuid, resume=True)` over UDS. Server-side substrate-claim verification (§Verification at connection-accept above) gates the resume.

Watcher (excluded from M3) keeps its current SDK path unchanged.

### PATH 2.8 explicit rejection for substrate-anchored UUIDs

`src/mcp_handlers/identity/resolution.py:679-694` (PATH 2.8) is gated for substrate-anchored UUIDs: the handler queries `core.substrate_claims[token_agent_uuid]` and if a row exists, returns an explicit error pointing the caller at the UDS path:

```
This agent is substrate-anchored. Token-based resume is no longer accepted for
this UUID. Connect via UNITARES_UDS_SOCKET to use substrate attestation.
```

Non-substrate-anchored UUIDs continue through PATH 2.8 unchanged (S1-a TTL applies).

## Sequencing (v2)

1. **Schema + enrollment CLI.** Migration creating `core.substrate_claims`. `scripts/ops/enroll_resident.py` (renamed from `.sh` during PR1 implementation) with the loud-warning behavior on user-writable binary paths.
2. **`peer_attestation.py` module + macOS backend** with fixture-tested parsers. Includes start-time read.
3. **UDS listener + `SessionSignals.peer_pid` extension + verification at connection-accept** (steps 1-7 above). All verification calls via `run_in_executor`. In-process `verified_pairs` cache for PID-reuse mitigation.
4. **PATH 2.8 substrate-anchored gate** in `resolution.py:679` returning explicit-rejection with UDS path hint.
5. **SDK: UDS transport detection + token-skip in `_save_session`** for substrate-anchored mode.
6. **Per-resident migration** for Vigil → Sentinel → Chronicler. For each: operator runs `enroll-resident.sh`; sets `UNITARES_UDS_SOCKET` in resident's plist; restarts resident; verifies onboard succeeds via UDS; deletes existing anchor file; lets resident write new UUID-only anchor on next start.
7. **Regression tests:**
   - External HTTP process presenting a copied substrate-anchored anchor token via PATH 2.8 → rejected with UDS-path hint message.
   - UDS connection with kernel-attested peer PID matching enrollment → accepted.
   - UDS connection with peer PID mismatching `expected_launchd_label` → rejected.
   - UDS connection with peer PID matching label but mismatching `expected_executable_path` → rejected.
   - UDS connection where peer PID matches but `start_tvsec` differs from cached value → rejected (PID-reuse simulation).
   - Enrollment CLI with binary in user-writable directory emits warning + deployment-risk audit event.

Each step is single-concern; ships in order. Total scope ≈ 6 PRs. Watcher excluded from all steps.

## Open questions deferred (not blockers)

- **S19-b Watcher follow-up.** If Watcher's leak surface becomes pressure, a separate row for Watcher-specific resume gating. Could revisit M3-hook (executable-path-hash) at that point. Tracked as future work, not v2 scope.
- **Linux backend for `peer_attestation.py`.** Stubbed with `NotImplementedError`. Activated when first Linux substrate-anchored resident lands.
- **Hardened deployment path.** Migrating residents to `/opt/homebrew/bin/` (or equivalent root-owned path) closes A2-escalated. Operator decision; not blocked on code.
- **Cross-host substrate attestation (Lumen).** UDS doesn't apply Pi → Mac. Generalizing M3 across hosts is a separate design problem if needed.

## Non-goals (unchanged)

- Closing A3 (root-equivalent attacker — outside threat model).
- Migrating Lumen or session-like agents.
- Retiring `continuity_token` for non-substrate-anchored agents (S1's job).
- Generalizing to Linux residents (deferred to follow-up).
- Replacing operator-side secret management for non-resident credentials.

## Reviewer gate state

- **Mechanism selection: SATISFIED 2026-04-25.** M3-v2 is the chosen mechanism. Council pass produced parallel architect + adversary reviews. All Q1-Q5 forcing questions answered. All adversary-reviewer critical issues addressed in v2 (binary-path constraint added; PID-reuse mitigation made required; Watcher excluded; operator pre-seed default). All architect verdict constraints addressed in v2 (Watcher split → exclusion; pre-seed enrollment; step 2a PID-reuse mitigation now step 3 sub-7 in sequencing).
- **Implementation correctness: NOT YET GATED.** That gate lives in the implementation row's regression suite (§Sequencing step 7) and a follow-up adversary-test run on the actual implementation. Implementation row may now open.

## Implementation row authorization

S19 v2 ratified. Code work may begin in a worktree following standard project conventions (worktree per memory `feedback_worktree-for-code-work.md`; ship via `scripts/dev/ship.sh` per `feedback_ship-sh-routing.md`). The `WIP-PR:` field in the S19 plan.md row should be stamped when the first implementation PR opens.
