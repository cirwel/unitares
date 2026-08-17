# Roadmap

**Last reviewed:** 2026-08-16

This roadmap states priorities, not delivery dates. Deployed behavior is defined
by releases and canonical documentation, not by this file.

## Now — make the evidence path independently interpretable

- Run a validation cohort of **independent draws** with a preregistered protocol,
  reporting effective independent clusters rather than nominal agent count, and
  publish negative or inconclusive results
  ([#1607](https://github.com/cirwel/unitares/issues/1607)).

  An operator other than the maintainer is **one route to independence, not a
  precondition**. The 2026-07-30 individuality read failed at effective n=4
  against a nominal n=7 because three of the eligible "agents" were one
  replicated Raspberry Pi (E r=0.952, I r=0.932, S r=0.998, byte-identical rows
  at matched timestamps). That is an independence-accounting problem, and
  recruiting a stranger does not fix it while the accounting stays wrong.
  Heterogeneous model families and machine-checked task corpora under this
  operator supply independent draws; synthetic traffic still does not.

  This item gates **efficacy** claims under "Later". It does not gate the
  instrument-frame work — reliability, faithfulness under intervention, and
  calibration — which needs neither external labels nor another operator
  (see `docs/ontology/eisv-proprioception-contract.md`, "Sensory class split").

- **Multi-principal trust** is a separate claim with its own evidence path:
  whether identity, attestation, and enforcement survive a principal who does
  not share this authority. The cohort above does not establish it, and it does
  not gate the cohort. See [`docs/SCOPE_AND_THREAT_MODEL.md`](docs/SCOPE_AND_THREAT_MODEL.md).

## Next — reduce maintenance and integration friction

- Extract a few low-risk package boundaries from the flat `src/` and `tests/`
  layout without a mass rewrite
  ([#1606](https://github.com/cirwel/unitares/issues/1606)).
- Maintain and test the compatibility map for the server, SDK, governance
  plugin, and host adapters.

## Recently shipped (see releases and the changelog for detail)

- `unitares-sdk` 0.1.0 published to PyPI via trusted publisher.
- Versioned multi-architecture container images with SBOM and build provenance,
  published from the release workflow.
- Documentation lifecycle review: archive/split candidates adjudicated
  ([#1605](https://github.com/cirwel/unitares/issues/1605)); doc health gate green.
- Watcher tests isolated from live operator state
  ([#1608](https://github.com/cirwel/unitares/issues/1608)).

## Later — only after independent evidence

- Multi-operator and mutually distrustful-governor experiments.
- Broader policy enforcement or efficacy claims.
- Stabilization commitments for a 1.0 server/API surface.

## Standing non-goals

- Replacing model evaluations, sandboxes, or action-level guardrails.
- Treating EISV as a universal ethics, correctness, or outcome score.
- Converting paper mathematics into production claims without deployed and
  reproducible evidence.

Priority changes should be proposed in an issue with the evidence, affected
users, and tradeoffs. The maintainer updates this page when the ordering changes.
