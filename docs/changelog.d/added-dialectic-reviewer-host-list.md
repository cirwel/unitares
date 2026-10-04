- **dialectic reviewer: an ordered host list.** `UNITARES_DIALECTIC_REVIEWER_HOSTS`
  names up to three reviewer hosts (`codex`, `claude`, `antigravity`,
  `external`) in the order the orchestrated reviewer tries them. A host that
  returns no reply is skipped for the next; the order is never changed. After
  the list, the local model answers and may object but not approve. An
  approval now needs `vouched: true` in its provenance (a listed host that may
  approve answered, or no list is set and the local model is the reviewer), so
  a verdict whose provenance does not say so is withheld. The local endpoint
  cannot be listed, by name or by URL; an invalid list calls no host. The
  repair and every reconsideration go to the host that answered first. Each
  verdict records the list, a configuration digest, every attempt and the
  answering host's family. `UNITARES_DIALECTIC_REVIEWER_HOST` still works as
  a one-item list; with neither set, behavior is unchanged. Step 1 of
  `docs/proposals/active/dialectic-reviewer-hosts-v0.md`; it does not yet
  check model-family independence or share cooldowns.
