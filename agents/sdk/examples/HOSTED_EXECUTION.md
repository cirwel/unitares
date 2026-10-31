# Hosted execution: a tested contract, not a deployed adapter

`hosted_execution.py` uses the existing `GovernanceClient`; it is not a second
SDK or a host integration. There is currently **no verified hosted-dot
start/end hook or installation surface in this implementation**. Nothing here
registers a resident, mints on import, persists a credential, or activates a
service. Do not run the example against a server until the host contract and
identity-write authorization are established.

## Three different continuities

- **Enduring assistant:** its display name and durable work/decisions. These are
  context, not proof of a particular running process. Keep durable knowledge in
  shared memory; do not use an evaluation episode's UUID as an enduring anchor.
- **Execution:** a host-established running instance. It gets a fresh ordinary
  process identity, not `GovernanceAgent`'s privileged resident-anchor pattern.
- **Binding:** the SDK holds `uuid`, `client_session_id`, and any returned proof
  only in memory. Later calls automatically receive the session ID. Neither a
  conversation ID nor a restored credential proves the same live execution.

## Integration prerequisites (still unmet for hosted dots)

The actual host must supply an authenticated/otherwise verified execution
start, define what ends that execution, retain one client inside it, and expose
an authorized place to run the SDK. A boolean passed to the example does not
establish those facts; it is the host caller's assertion that they were already
verified. Unknown boundary fails before even connecting. No default infers a
boundary from a room, message, timestamp, display name, or memory.

The example processes an async stream of **meaningful, agent-authored reports**
inside one verified execution. It verifies the first session-bound read before
writing a report, skips blank reports, and sends no idle heartbeat or synthetic
start/end check-in. Generated summaries belong to `substrate_interpretation`,
not this agent-report stream. The host must supply only useful milestones,
usually zero or one report per turn. Reports use `call_tool("sync_state", ...)`,
not the resident convenience `checkin()`: a display-name collision with a roster
entry must not produce `resident:/` substrate observations or resident recovery.
Report writes also disable transport retries; ambiguous delivery needs
reconciliation, not replay. Finishing the stream closes the transport;
it does not establish or attest that the host process exited.

## Failure and restart contract

| Event | Required behavior |
|---|---|
| Boundary unknown | No connection, onboarding or check-in. |
| Known fresh execution | Call example once; fresh onboard with `retry=False`. |
| Mint timeout / lost response | Stop. Outcome is uncertain, not “not minted.” Reconcile with host/server before any new attempt; never automatically rerun the example. |
| Missing/mismatched binding read | No reports; stop and reconcile, never mint as repair. |
| Context compaction, same verified execution | Keep the same in-memory client and binding. Do not call the example again. |
| Reconnect, same verified execution | Reconnect the same SDK client; recheck binding before further writes. Do not remint. The example deliberately has no reconnect loop. |
| Verified fresh execution after restart | New client, fresh identity. Prior work may be read as context, not restored as proof. |
| Intentional succession from a proven finished predecessor | Existing `onboard(parent_agent_id=..., spawn_reason="explicit", force_new=True, retry=False)` supports it; this example intentionally does not infer or send lineage. |

`onboard(retry=False)` disables the SDK's automatic transport retransmission;
it is **not** an exactly-once guarantee. The host must deduplicate lifecycle
callbacks and keep uncertain attempts pending for reconciliation. Neither this
example nor the SDK can prevent an external caller from starting it twice.

## Offline verification

From the repository root (with SDK test dependencies installed):

```sh
python -m pytest agents/sdk/tests/test_hosted_execution_contract.py
```

These tests mock the transport, not the SDK session-injection implementation.
They establish wire arguments and local failure behavior, not live host
provenance, server authorization, restart detection, deployment or residency.
