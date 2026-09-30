# Local full PR review

Codex and Claude can use the same fresh Ollama reviewer from any UNITARES checkout:

```sh
scripts/dev/review-local.sh --pr 2585
scripts/dev/review-local.sh --pr 2585 --second-family --record
```

Gemma4 is the validated default on this host. `--second-family` also selects Gemma4,
which the existing review gate recognizes as Google family. Qwen currently has no
recognized family credit. Both modes review the full diff with immutable repository
read/search tools; neither is a targeted fix verifier. A second family only completes
the family requirement when an independent passing first-family review also exists.

Install Ollama and the models before running. No paid API or external endpoint is
used. Override installed model names per repository:

```sh
git config review.localModel gemma4:latest
git config review.localSecondModel gemma4:latest
git config review.verifier ollama:gemma4:latest
```

The last setting serves the existing **targeted fix verifier**, not full-review
credit. The runner verifies the installed model architecture before assigning family credit;
renaming a different architecture cannot earn Google credit. Pass `--model NAME` for a single run.

`--repo PATH` selects the repository without switching branches. The runner fetches
the PR head/base, sends the complete diff, and allows only read/search against the
frozen head. It never edits source, runs repository commands supplied by a model,
or activates hooks/skills. Model output and provenance stay under the repository's
common Git directory in `local-reviews/`. Source is sent only to a loopback Ollama
HTTP endpoint; proxies and redirects are disabled. One local review runs at a time
across repositories to avoid simultaneous large-model loads.

Default context is 131,072 tokens for both models on the 128 GiB review host with a conservative byte-based admission budget;
`--context 131072` supports larger reviews if the model and available memory permit.
Default total generation budget is 1,800 seconds (`--budget`, maximum 3,600).
Context overflow, missing source inspection, incomplete generation, malformed output,
and tool/time exhaustion fail as UNREVIEWED. No truncated full diff earns a pass.

Review without `--record` first when trying a new model. `--record` requires the provenance-aware gate from PR #2596 in the runner checkout and explicitly posts
through the canonical gate renderer, after verifying both PR head and base still
match. Exit codes: 0 = CLEAN, 1 = FINDINGS, 2 = UNREVIEWED/error. A CLEAN exit does
not itself assert CI green, family coverage, readiness, or authorization to merge.
Assess findings independently and follow the normal gate/disposition workflow.

A local full review consumes a review attempt; honor repository review budgets and
operator pauses. This command does not resume scheduled sweeps. Never use it to
manufacture a missing review or replace a required reviewer with author reasoning.

For a shared shortcut, set `git config alias.review-local "!ABSOLUTE/PATH/TO/scripts/dev/review-local.sh"`.
Keep that checkout available until the shortcut is repointed to a merged checkout.

The canonical gate is imported from the runner checkout, never from the PR being reviewed.
After #2596 merges, update that checkout or repoint the shortcut to a checkout containing both changes.

Qwen is opt-in: `git review-local --pr NUMBER --model qwen3-coder-next-64k:latest`.
The installed Qwen model repeated identical tools in the initial pilots; Gemma completed
a full review of #2585. Repeating an identical tool more than twice fails UNREVIEWED.
Gemma supplies Google credit alongside a passing Codex/OpenAI or Claude/Anthropic review;
two Gemma runs are still one family. Tool messages follow the [Ollama API](https://docs.ollama.com/capabilities/tool-calling).
