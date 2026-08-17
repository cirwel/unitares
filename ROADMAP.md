# Roadmap

**Last reviewed:** 2026-08-16

This roadmap states priorities, not delivery dates. Deployed behavior is defined
by releases and canonical documentation, not by this file.

## Now — make the evidence and adoption path independently usable

- Run an independent-operator validation cohort with a preregistered protocol
  and publish negative or inconclusive results ([#1607](https://github.com/cirwel/unitares/issues/1607)).
  This item gates everything under "Later".

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
