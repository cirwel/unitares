# Two-governor federation tracer

This isolated research harness supplies a narrow piece of evidence for the
accountable multi-principal agent testbed proposal. It launches two independent
OS processes. Each process creates and retains its own Ed25519 private key. The
two domains exchange only public JWKs and explicitly pin one another; they do
not share a private signing key or administrative controller.

The trace exercises:

- a valid, audience- and scope-bound cross-governor voucher;
- voucher replay, signature forgery, wrong audience, and expiry;
- an authentic but false voucher, whose origin verifies but whose evidence
  digest disagrees with the recipient's observation;
- a holder-bound authorization plus request-specific proof of possession; and
- rejection of a copied authorization token without the holder private key,
  followed by rejection of a replayed valid proof.

Run the tracer and its focused tests from the repository root:

```bash
python3 -m scripts.demo.federation_tracer.tracer \
  --output docs/proposals/accountable-testbed-federation-trace.json
python3 -m pytest tests/test_federation_tracer.py -q
```

The harness is not production authorization code and is not a full multi-host
deployment. A signature authenticates an issuer; it cannot establish that the
issuer's claim is true. The evidence-mismatch case is therefore reported as a
policy/evidence rejection, not a cryptographic forgery rejection.
