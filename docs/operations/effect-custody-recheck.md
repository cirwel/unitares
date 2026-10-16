# File-write custody recheck: bounded revocation experiment

The October 5–12 frontier brief proposed revoking authority between planning
and execution. This experiment applies that idea to the existing file-write
executor, without adding a standing CapabilityLease or changing live flags.
See the [brief's repository mapping](../ontology/frontier-brief-2026-10-12.md).

## Reproduction and result

On base `543bab63c`, three deterministic interleavings all returned success
and wrote the test payload after losing the originally acquired lease:

1. Force-release that lease while the governance veto call is pending.
2. Expire it during the veto, without waiting for the reaper.
3. Force-release it and acquire a replacement for the same surface and holder.

The tests use the existing `governance_test` database, temporary test files,
and an injected veto client. The real lease store, file executor and audit
store are exercised; the external governance service is simulated. The veto
returns `allow` after the custody change. This isolates custody freshness from
the separate identity-lifecycle fix in PR #2840.

The dispatch previously retained canonical surface names but did not reread
the acquired lease rows after the veto. It now preserves those rows and checks
the exact lease ID, surface and holder against the store. Released or missing
rows return `revoked`; elapsed expiry returns `custody_expired`, using the
database wall clock. Query errors fail closed as `custody_unavailable`.

The HTTP response is 409 for lost/expired custody and 503 when custody cannot
be checked. Before executor dispatch, refusals are recorded in
`governed_effect.execute` with `lease_ids` and `custody_check = after_veto`.
An audit-write failure returns `persist_failed`; it does not authorize a write.
These new refusal statuses do not consume the idempotency key: retries must
acquire fresh custody and pass the veto again. Cleanup only releases the old
lease ID, leaving a replacement untouched.

Run the experiment:

```sh
cd elixir/lease_plane
mix test test/governed_effect_test.exs --only custody_revocation
```

Do not point this suite at the live governance database. The default test
configuration uses `governance_test`. The three tests now exercise the HTTP
route, assert unchanged bytes and no payload row after refusal, check the
durable refusal and original lease ID, and verify replacement ownership or
a successful retry with fresh custody. Existing successful commit, dry-run,
compensation and recovery tests supply companion coverage.

## Limits and the next experiment

This fixes custody changes completed before the post-veto check. It does
**not** make the check and the filesystem write atomic. Payload persistence,
pre-image capture and the OS write happen afterwards; a release or expiry in
that interval is not excluded. A lease row is not a filesystem fencing token.
The current file-write executor and compensation path still need a design
that orders revocation against the actual mutation, with a defined point
after which an in-flight operation is allowed to finish.

The next experiment should pause immediately before the OS write, complete
revocation, allow a successor to acquire the surface, and then resume the
old writer. Its deciding standard must cover the old writer, compensation,
crash recovery and successor ownership. Holding a database transaction open
around a file write alone is insufficient: connection loss can release the
lock while the process can still mutate the file, and rolling back the
transaction could erase the recovery pre-image after a real write.

No result here establishes protection for arbitrary shell or network tools,
the agent-spawn executor, cross-tier capability grants, policy-version or
safeguard-profile binding, or live deployment. Those are separate scopes.
