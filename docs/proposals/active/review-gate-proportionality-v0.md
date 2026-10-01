# Review gate proportionality (v0)

Status: Proposal. Item 3 (the operator waiver) is built in this PR, bounded as
described in `docs/operations/github-workflow-conventions.md`; items 1, 2 and 4
are not built.

Date: 2026-09-30

## 1. Problem

The gate is stricter than the risk it guards in three places, and on
2026-09-30 that stopped three finished PRs for reasons unrelated to their code.

- **Path match stands in for a risk judgement.** #2593 removes tool aliases. It
  touches `src/agent_identity_auth.py` to change one help string
  (`list_agents` to `agent(action='list')`), so the whole PR needs two model
  families. A reviewer that read the entire diff returned CLEAN; the gate still
  blocked it.
- **Every finding blocks.** #2571 took three rounds on nine P2 findings. Two
  were defects in the previous round's fixes; the first round also caught a real
  trust mismatch. P0/P1 are worth a hard block; the P2 tail is what made the
  loop long.
- **The second family depends on quota.** On that day Gemini was over quota,
  Claude subprocess access was disabled, and the local Gemma reviewer refused
  any diff over about 130 KB. A 400 KB alias removal can never be reviewed
  locally.

Underneath all three: PR size. A 3,400-line diff gets a fresh set of edge cases
on every round.

## 2. Proposal

1. **P0/P1 block; P2 and below are recorded.** They stay on the PR as
   findings, but leave the check green. (#2596 already moves this way.)
2. **Path match raises the bar only when the diff changes behaviour on the
   path.** A diff whose hunks on a listed path touch only comments, docstrings
   or string literals outside an auth decision asks for one family plus a
   recorded note naming the hunks. Anything else keeps two families.
3. **Operator waiver as a first-class record.** `review.sh waive --reason TEXT`
   posts a record the gate accepts in place of the missing family, names the
   operator account, and shows on the PR. It is the existing human-review path
   made explicit; it does not let an agent waive for itself.
4. **Size budget.** A PR over a stated diff size (proposal: 1,500 changed lines
   outside generated files and tests) is asked to split before review, since a
   diff the local reviewer cannot read also cannot be checked by a second
   family without cloud quota.

## 3. What does not change

- `scripts/dev/review_policy.json` is still read from the base ref, so a PR
  cannot remove itself from the list.
- Two families stay required for changes that alter an authentication or
  authorisation decision.
- An agent never waives its own PR.

## 4. Open questions

- Who decides "only comments or string literals": a script over the hunks, or
  the operator's waiver? A script is cheaper and auditable; a waiver is simpler.
- Whether the size budget counts tests and generated docs.

## 5. Build order

Item 3 is built. The others are not started. Each step edits `review_gate.py` or `review_policy.json`, both of
which are on the second-family list, so each needs its own two-family review.
Land after #2596 so the gate is edited once.
