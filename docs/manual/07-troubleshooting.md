# 7 · Troubleshooting & FAQ

[← Operating](06-operating.md) · [Manual index](README.md)

The fuller, symptom-indexed guide is [`../guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md); install-specific failures are in the [playbook's troubleshooting table](../install/PLAYBOOK.md#troubleshooting). This chapter covers the failures and questions new users actually hit.

## 7.1 Quick diagnostics

```bash
# Is the server alive?
curl -s http://127.0.0.1:8767/health/live          # → {"status":"alive"}

# What's bound to the port?
lsof -i :8767

# A full health snapshot via the tool surface
curl -s -X POST http://127.0.0.1:8767/v1/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"tool":"health_check","arguments":{}}' | python3 -m json.tool
```

## 7.2 Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `Address already in use` on `8767` | Server already running | `lsof -i :8767` → kill it, or start with `--port 8768` (and the matching Docker host-port vars) |
| `pg_isready: no response` | Postgres not started | `brew services restart postgresql@17`, wait, retry |
| `role "postgres" does not exist` | Homebrew Postgres uses *your* username as superuser | Drop `postgres:postgres@` from the DSN, or create the role explicitly |
| `relation "agents" does not exist` on first call | Schema not applied | Re-run the schema `psql` steps ([§2.2.2](02-install.md#222-create-the-database-and-apply-schema)) |
| `extension "age" is not available` | AGE built against the wrong `pg_config` | Verify `$PG_CONFIG` points at your PG 17 and rebuild AGE |
| `make` fails on AGE with `bison` errors | Apple's old bison shadows Homebrew's | `brew install bison && export PATH="$(brew --prefix bison)/bin:$PATH"` |
| Dashboard loads but is empty | Expected — no agents yet | Run one `onboard` / `start_session` call |
| Remote client reports a generic auth / "API key" error | **HTTP 403 from the Host allowlist**, which runs *before* auth | Add the host to `UNITARES_MCP_ALLOWED_HOSTS` (bare *and* `:*` forms) and restart ([§3.5](03-running-the-server.md#35-exposing-beyond-loopback)) |
| Remote client gets `401` | Bearer gate is on (expected once a key is set) | Paste the same `UNITARES_MCP_BEARER_TOKENS` value into the client's API-key field |
| Server starts but verdicts lack ODE diagnostics | `UNITARES_DISABLE_ODE=1` is set | Intentional in signal-only mode; unset it to restore the math model |

## 7.3 FAQ

**Do I have to change my agent's code?**
No. The two-call loop ([chapter 4](04-integrating-agents.md#41-the-default-workflow)) is the simplest path, but the [governance plugin](https://github.com/cirwel/unitares-governance-plugin) mounts Claude Code / Codex via hooks with no loop edits, and the [host adapter](https://github.com/cirwel/unitares-host-adapter) covers other clients.

**Does it block or sandbox my agent?**
No. UNITARES is a *state layer*, not a validator or sandbox. It returns a verdict the agent reads and acts on; it does not intercept actions. Enforcement is your agent's (or a separate guardrail's) job.

**Do I need Redis?**
No. Redis is an optional session cache; the server falls back gracefully without it.

**Do I need the ODE / `governance_core`?**
No. Set `UNITARES_DISABLE_ODE=1` to run the behavioral-EISV verdict path alone. The ODE is a parallel diagnostic lens, not the verdict driver.

**Why did a stable agent suddenly get a low score?**
After ~30 check-ins, scoring is *self-relative* (z-score from the agent's own baseline). A deviation from its own norm can register even while absolute health is fine — but absolute basin-health gating means in-basin deviation is treated as information, not danger. If it `pause`d, check `margin` and the named risk components.

**Can an agent game the signal by reporting high confidence?**
It can inflate the *number*, not the *outcome*. Confidence is scored against objective results (tests, exit codes, lint) via `record_result`; persistent overconfidence lowers Integrity. Feed real outcomes back for this to work. ([§5.4](05-reading-the-signals.md#54-calibration-why-the-signal-resists-gaming).)

**How much should I trust EISV?**
As far as the falsifiability harness on *your* data lets you. The honest current read is a weak early signal with no demonstrated prevention — run [the harness](../REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) before relying on it. ([§5.7](05-reading-the-signals.md#57-dont-trust-these-numbers-blindly).)

**Two processes, one agent — how do I keep identity straight?**
A fresh process mints a fresh UUID. Pass `client_session_id` for writes within the same process; declare a real handoff with `parent_agent_id` + `spawn_reason="new_session"`. Never pass a bare UUID as proof of selfhood. ([§4.3](04-integrating-agents.md#43-identity-the-one-rule-that-matters).)

**Is it safe to expose on the internet?**
Only with both gates set — a Host/Origin allowlist *and* an auth gate (bearer or OAuth). The defaults are loopback-only and the threat model assumes a single-operator fleet; multi-tenant/public use needs a harder posture. ([§6.3](06-operating.md#63-security-posture).)

## 7.4 Recovery procedures

Server health, log locations, process inspection, and database reset steps are in [`../guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md#recovery-procedures). Do not run `DROP`/`TRUNCATE`/`DELETE` on the governance database without a backup and deliberate intent.

## 7.5 Getting help

- Symptom-indexed guide: [`../guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)
- Architecture disputes: [`../dev/CANONICAL_SOURCES.md`](../dev/CANONICAL_SOURCES.md) (runtime code wins)
- Security issues: [`../../SECURITY.md`](../../SECURITY.md)
- The paper and the wider stack: [`../../README.md#the-cirwel-stack`](../../README.md#the-cirwel-stack)

---

[← Operating](06-operating.md) · [Manual index](README.md)
