# Consult exact-brief replay — 2026-10-14

The original two-sentence request reproduced the reported verbosity failure:
three old-wrapper runs returned multi-section critiques of 474–571 words;
three revised-wrapper runs returned two sentences each (49–58 words).
Both wrappers also passed the three previous synthetic format checks.
This supports a narrow prompt repair on this model and brief, not a general
failure-rate estimate or a claim about advisory quality.

## Recovered evidence

The operator supplied the original call and saved local answer. Its 525-word
response matches SHA-256
`b35477dc835bbea0c3fbc69b89eaa82b07b8d2d23b1e2c18768b2ddb83fe2a71`.
The response refers to “the two-sentence explanation” as supplied evidence,
consistent with treating the imperative brief as material to critique.
That interpretation is supported by the text; the model's internal cause is
not observable. The earlier [synthetic probe](consult-constraint-probe-2026-10-13.md)
used briefs beginning with `Proposal:`, which did not expose this failure.

Exact original brief, `purpose="critique"`:

```text
In two sentences, explain why having 5 overlapping model-consultation tools (consult, call_model, delegate_inference, list_inference_hosts, describe_inference_host) plus request_review creates cognitive overhead for an agent.
```

The brief and old constructed prompt match the supplied SHA-256 digests:

- Brief: `5d1915e64e7d3cd640e8ae509bb787c5dc417c01c3e328317a409fd9c40bac6f`
- Old prompt: `187844809e17409c26e7f4bc9f3e2b844fdc6f3134be6faa463900a23626d63f`

## Method and prompt change

Twelve local HTTP calls to `http://localhost:11434/v1/chat/completions`,
`gemma4:latest`, one user message, `temperature=0.7`, `max_tokens=2048`.
Before running, fixed the original-brief seeds at 42, 43 and 44, followed by
one pair for each of the prior three synthetic briefs with seed 42. Alternated
pair order: before/after, after/before, before/after, and so on. The criteria
were the requested formats: two sentences for the original; the exact
sentence, bullet and JSON constraints specified in each synthetic brief.
Sentence counts and structure were inspected manually; whitespace word counts
and JSON shape/value lengths were checked programmatically.

The original call's seed was not supplied. These runs reproduce its brief,
wrapper and source-configured temperature/token limit, not its random state.
Requested seeds were not independently verified as deterministic. All twelve
calls ended with `finish_reason="stop"` and nonempty answer content. Scoring
uses answer content, not the separate reasoning field. The probe made no
cloud calls or governance updates. It measured format adherence, not whether
the generated claims about tool overhead are true.

Before, from `af1af8572` (identical to `54a282044` for prompt construction):

```text
Critique the proposal as advisory model evidence. Identify concrete risks, counterarguments, and improvements; do not claim a review verdict.

Consultation brief:
<brief>
```

After, verified byte-for-byte against `_constructed_prompt` in the fix:

```text
Follow the request in the consultation brief, including its length, format, and scope constraints for the answer content. Use the consultation purpose within those constraints; do not add unrequested sections.

Consultation purpose: Provide a critical perspective as advisory model evidence. Identify concrete risks, counterarguments, or improvements where relevant; do not claim a review verdict.

Consultation brief:
<brief>
```

The common constraint instruction applies to all four purposes. Critique no
longer assumes that every brief is a proposal, or requires all three categories
regardless of scope. Host adapters still append their terminal-answer contract;
brief constraints apply to the answer content inside that envelope. This is a
prompt correction, not an enforced sentence limit, truncation, or retry policy.
Input/output schemas, routing, authority and audit hashing rules are unchanged.
Prompt hashes naturally change with the actual prompt.

## Results

| Case | Before | After |
|---|---|---|
| Original, seed 42 | Fail: 474 words, multiple sections | Pass: 2 sentences, 53 words |
| Original, seed 43 | Fail: 519 words, multiple sections | Pass: 2 sentences, 49 words |
| Original, seed 44 | Fail: 571 words, multiple sections | Pass: 2 sentences, 58 words |
| Prior two-sentence brief | Pass: 2 sentences, 54 words | Pass: 2 sentences, 60 words |
| Prior two-bullet brief, at most 40 words | Pass: 2 bullets, 27 words | Pass: 2 bullets, 26 words |
| Prior JSON-only brief, at most 15 words/value | Pass: 11 / 9 words | Pass: 14 / 12 words |

[Retained local replay data](consult-constraint-replay-2026-10-14.json) includes
all twelve answer contents, exact briefs, settings, order and prompt digests.
Model-generated claims are retained for inspection, not endorsed. The three
original-request samples share one brief/model; they are not independent
coverage of the broader consultation workload. Other purposes and cloud hosts
were checked deterministically for prompt routing and envelope preservation,
not evaluated for live model adherence in this probe.

## Claude malformed failure remains unresolved

The supplied Claude evidence is a failed consultation envelope: terminal
`adapter_status="malformed"`, exit status 0, no raw model reply. A scoped GET
for the reported execution returned HTTP 404 on 2026-10-14; no original output
was recovered. Neither Markdown fences nor missing context is established as
the cause. No replay is represented as the original output.

The supplied reconstructed response contract also differs from the repository
source at `54a282044` and `af1af8572`. `_terminal_answer_prompt` appends a
contract with exactly `schema`, `status`, `answer`, and statuses `complete`,
`needs_input`, `declined`. It does not offer `incomplete` plus `reason`.
This verifies the source contract, not the exact deployed bytes of that
historical call. The strict parser and the policy against retrying malformed
output on a second peer remain unchanged. The already-merged diagnostic fix
in #2788 preserves a bounded unvalidated excerpt on future full-mode failures.
