# Knowledge graph: bi-temporal fact validity (deferred)

**Created:** 2026-06-02 · **Status:** Parked — documented idea, not built, not a roadmap item

Moved here on 2026-10-11 from `skills/knowledge-graph/SKILL.md`, where it had
been recorded with the on-demand topic synthesis change (#557). The text is
unchanged in substance.

A first-class notion of *when a fact became true/false* (valid-from / valid-to
plus observation time, per the Graphiti/Zep bi-temporal model) was considered and
**deliberately deferred**. It is a substrate-level change — migration, AGE query
rewrites, and every read path having to reason about time — for a payoff
(point-in-time reconstruction, automatic invalidation) that is speculative for
current usage. The existing `superseded` status + `created_at` is an ~80%
substitute.

**Resume signal:** a concrete failure — an agent acting on a stale fact in a way
that actually bites. Until then this stays a documented idea.
