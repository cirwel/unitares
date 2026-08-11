# Roadmap

**Last reviewed:** 2026-08-11

This roadmap states priorities, not delivery dates. Deployed behavior is defined
by releases and canonical documentation, not by this file.

## Now — make the evidence and adoption path independently usable

- Run an independent-operator validation cohort with a preregistered protocol
  and publish negative or inconclusive results ([#1607](https://github.com/cirwel/unitares/issues/1607)).
- Publish the first `unitares-sdk` package after PyPI trusted-publisher setup;
  retain a version-pinned Git install until that release exists.
- Publish versioned multi-architecture container images with SBOM and build
  provenance from the release workflow.
- Resolve the current documentation archive/split candidates so shipped,
  proposed, and historical work remain visibly distinct
  ([#1605](https://github.com/cirwel/unitares/issues/1605)).

## Next — reduce maintenance and integration friction

- Isolate watcher tests from live operator state
  ([#1608](https://github.com/cirwel/unitares/issues/1608)).
- Extract a few low-risk package boundaries from the flat `src/` and `tests/`
  layout without a mass rewrite
  ([#1606](https://github.com/cirwel/unitares/issues/1606)).
- Maintain and test the compatibility map for the server, SDK, governance
  plugin, and host adapters.

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
