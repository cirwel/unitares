# Preliminary Trace: Two-Governor Federation Controls

**Captured 2026-08-03 against an isolated, public research harness.** The
machine-readable capture is
[`accountable-testbed-federation-trace.json`](accountable-testbed-federation-trace.json),
and the implementation is in
[`scripts/demo/federation_tracer/`](../../scripts/demo/federation_tracer/).
All ten registered checks passed in the captured run.

This trace replaces the 2026-06-30 single-deployment trace as evidence for the
proposal's federation claim. That earlier exercise showed useful UUID and
lineage behavior inside one UNITARES deployment, but it did **not** establish
independent governors, a cross-governor signature, or resistance to use of a
genuinely copied target credential. This narrower harness tests those claims
directly and states the remaining gap explicitly.

## What ran

The harness launched two distinct OS processes:

| Administrative domain | PID in captured run | Ed25519 key ID |
|---|---:|---|
| `principal-alpha.example` | 42182 | `7729a663f56f9410` |
| `principal-beta.example` | 42183 | `9878e90d6656914f` |

Each process generated and retained its own private key. The domains exchanged
only public JWKs and pinned one another explicitly. There was no shared private
signing key. Public-key pinning is the trust bootstrap; the claim is therefore
"no shared private signing root or administrator," not "no trust root."

The exchanged voucher is a compact JWS signed with Ed25519 and binds the
issuer, audience, subject, scope, nonce, validity window, and SHA-256 digest of
the stated effect. The recipient verifies the signature using its locally
pinned issuer key and separately compares the evidence digest with its own
observation of the proposed effect.

For authorization, the issuing governor binds the token to a holder public key
using a JWK thumbprint. Every effect request carries a fresh holder signature
over the authorization ID, audience, method, path, body digest, nonce, and
timestamp. This is a proof-of-possession fixture, not a bearer-token equality
check.

## Captured results

| Case | Origin authentic? | Evidence consistent? | Decision |
|---|---:|---:|---|
| Valid cross-governor voucher | yes | yes | accepted |
| Replayed voucher | yes | not re-evaluated | rejected: `replay` |
| Payload changed without re-signing | no | not evaluated | rejected: `invalid_signature` |
| Voucher for another audience | yes | not evaluated | rejected: `wrong_audience` |
| Expired voucher | yes | not evaluated | rejected: `expired` |
| Legitimately signed false voucher | **yes** | **no** | rejected: `evidence_mismatch` |
| Holder-bound effect request | yes | request-bound proof valid | accepted |
| Copied authorization token, attacker key only | yes | holder proof absent | rejected: `holder_key_mismatch` |
| Replayed valid holder proof | yes | request proof already used | rejected: `replay` |

The false-voucher row is the important epistemic boundary. Cryptography cannot
tell whether an authorized issuer is lying. Its signature verifies. The
recipient rejects the voucher only because the signed evidence digest disagrees
with recipient-observed evidence. Future metrics must therefore report at least
two quantities: forged-origin acceptance and authentic-but-evidence-inconsistent
acceptance. Calling both "attestation forgery" would hide the difference.

The copied-token row is similarly precise. The exact authorization token issued
to the legitimate holder is supplied by the attacker, but the holder private
key is not. The request is rejected because the attacker's public key does not
match the token's confirmation thumbprint. This supports only the claim that a
stolen token **alone** is insufficient; theft of both token and holder private
key remains out of scope for this control.

## Reproduce

From the UNITARES repository root, with the `full` and `dev` dependencies
installed:

```bash
python3 -m scripts.demo.federation_tracer.tracer \
  --output docs/proposals/accountable-testbed-federation-trace.json
python3 -m pytest tests/test_federation_tracer.py -q
```

Fresh keys and process IDs are expected on every run; the semantic checks and
reason codes are deterministic. The focused test suite asserts process/key
separation, all adversarial outcomes, the origin-versus-truth distinction, and
the stolen-token-without-holder-key case.

## What this de-risks—and what it does not

This trace shows that the proposal's minimum federation mechanism can be
implemented with separate governor processes and independently verifiable
public-key artifacts. It also supplies executable adversarial cases for replay,
tampering, audience binding, expiry, compromised-issuer evidence mismatch, and
holder-bound authorization.

It is **not** two organizations on separate hosts, a production authorization
system, a benchmark-scale population, or evidence that public-key discovery and
rotation work under institutional conflict. Those are funded-work claims. The
planned evaluation must add multi-host deployment, explicit key rotation and
revocation, protocol baselines, population/topology scaling, divergent
incentives, and arms-length replication.
