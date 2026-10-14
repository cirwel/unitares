# Consult critique constraint probe — 2026-10-13

A small matched local check following the reported overlong critique. This is
format-following evidence, not an evaluation of factual or advisory quality.

**Result at the time:** both the current and candidate wrappers met all three tested
constraints (3/3 each). The reported failure was not reproduced. No prompt
change is shipped on this evidence; the diagnostic and HF-readiness repairs
are independent of it. The original brief, raw response and sampling settings
from the reported failure were not available for an exact replay.

**Follow-up 2026-10-14:** the original brief and local response have now been
provided. The [exact-brief replay](consult-constraint-replay-2026-10-14.md)
reproduces the failure and evaluates a revised wrapper. The six-call result
below remains evidence about these synthetic briefs, not the original request.

## Method

Called the configured local OpenAI-compatible endpoint at
`http://localhost:11434/v1/chat/completions`, model `gemma4:latest`. Six calls:
three synthetic briefs, current/candidate pair for each. Both variants used
`temperature=0.7`, `seed=42`, `max_tokens=2048`, and one user message; the seed
was requested, not independently verified as deterministic. Order was current
then candidate for cases 1 and 3, reversed for case 2. Every call finished with
`finish_reason=stop` and nonempty answer content. Scoring uses that content,
not the separate reasoning field. No cloud calls or governance updates were
made by the probe.

Current wrapper from base commit `54a282044`:

```text
Critique the proposal as advisory model evidence. Identify concrete risks, counterarguments, and improvements; do not claim a review verdict.

Consultation brief:
<brief>
```

Candidate inserted this immediately before the blank line:

```text
Follow the brief's explicit length, format, and scope constraints for the answer content. The purpose does not require extra sections or a minimum length.
```

Criteria came from the briefs, before running: exactly two sentences without
headings/lists; exactly two bullets and at most 40 whitespace-separated words;
or JSON only with exactly the two named string keys and at most 15
whitespace-separated words per value. Sentences, bullets and absence of
headings were inspected manually; JSON was parsed and word counts computed.
This is one sample per condition on one local model, with no estimate of
failure frequency or evidence that the candidate is better.

| Constraint | Current | Candidate |
|---|---|---|
| Two sentences, no heading/list | Pass: 2 sentences | Pass: 2 sentences |
| Two bullets, at most 40 words | Pass: 2 bullets, 27 words including bullet markers | Pass: 2 bullets, 30 words including bullet markers |
| JSON only, two string values of at most 15 words | Pass: 11 / 9 words | Pass: 12 / 13 words |

## Exact briefs and answer content

The following are model outputs retained for format inspection, not endorsed
claims about the proposals.

### two sentences

Brief:

```text
Proposal: Cache all inventory counts for 24 hours to reduce database load. Critique this in exactly two sentences, with no heading or list.
```

Current:

```text
The fixed 24-hour cache duration introduces a significant risk of data staleness, potentially leading to overselling or incorrect operational decisions when inventory volumes fluctuate rapidly. A more robust approach would be to implement cache invalidation triggered directly by write operations (transactions) and restrict caching only to high-read, low-volatility items, rather than encompassing all counts.
```

Candidate:

```text
The primary risk is inventory data staleness, which could lead to overselling or operational miscounts if real-time adjustments are necessary, and a counterargument is that this caching mechanism fails to account for write-through invalidation logic. Improving the model requires implementing a time-to-live (TTL) mechanism that is dynamically reset upon any inventory transaction and considering a read-replica cache approach instead of a full, static cache.
```

### two bullets

Brief:

```text
Proposal: Retry every failed background job immediately without a retry limit. Give exactly two bullet points, at most 40 words total, with no heading.
```

Current:

```text
*   Risk of cascading failure and resource exhaustion due to immediate, unlimited retries.
*   Improvement: Implement exponential backoff and utilize a dead-letter queue (DLQ) for failed jobs.
```

Candidate:

```text
*   Risk of catastrophic resource exhaustion and system thrashing due to unthrottled, continuous retries.
*   Implement exponential backoff and a defined retry limit to manage transient failures without overloading services.
```

### json only

Brief:

```text
Proposal: Log complete customer requests indefinitely to debug production incidents. Return only a JSON object with exactly two string keys, "risk" and "improvement". Each value must be at most 15 words. No Markdown fences.
```

Current:

```text
{"risk":"Severe privacy exposure, massive storage costs, and escalating legal compliance liabilities.","improvement":"Implement scoped logging, anonymization, and strict data retention policies."}
```

Candidate:

```text
{"risk": "Indefinite logging violates privacy regulations and causes exponential, unmanaged data storage costs.", "improvement": "Implement structured logging, anonymize sensitive data fields, and enforce strict, time-bound retention policies."}
```
