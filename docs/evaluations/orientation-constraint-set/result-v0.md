# Diagnostic orientation constraint set — cohort v0 execution record

**Status: ABORTED BEFORE SCORED CALLS (NO REGISTERED RESULT).** Cohort v0
completed its full canary gate on 2026-08-24, but every canary failed the
registered response parser. The runner stopped before the first scored call, as
required by the frozen protocol. No effect estimate, interval, p-value, safety
rate, or per-family treatment effect exists for this cohort.

## Frozen inputs

- Protocol: [`orientation-constraint-set-preregistration-v0.md`](../../proposals/orientation-constraint-set-preregistration-v0.md), commit `d6dc0c79`
- Enrollment: [`enrollment-v0.json`](enrollment-v0.json), commit `e1d76782`
- Enrollment digest: `c861c91917c3168f3ec681bce8d89a05f032b238b4e01c138e9557e129daccd7`
- Implementation commit: `dac9113fc5483962f57705a8140e385cfc985ad0`
- Model: `gemma4:latest`, digest `371e604cf9bed754bb3a4a76379c7925bbe4cf2cf432aa7145ec21c6226f891f`
- Initial governed review: `20ae6cbd2cf02a5c`
- Post-abort governed review: `bd74c50504b0f79a`

## Execution outcome

The run started at `2026-08-24T23:21:39.554233Z` and stopped at
`2026-08-24T23:23:21.507417Z` with
`reason=canary_transport_or_parse_failure`.

| Measure | Observed |
|---|---:|
| Canary calls scheduled / completed | 16 / 16 |
| Arms | 8 constraint-set, 8 provider-envelope |
| Families | 2 canaries in each of 8 families |
| Transport failures | 0 |
| Parse failures | 16 |
| Parse error | 16 `empty_response` |
| Provider termination | 16 `done_reason=length` |
| Generated tokens | 320 on every canary (the enrolled cap) |
| Answer-channel content length | 0 on every canary |
| Canary latency | 4,768.854–7,709.877 ms; median 6,947.166 ms |
| Scored calls executed | **0 of 240** |

Because scoring never began, the registered result class is not `INVALID`,
`SAFETY_STOP`, `REDESIGN_LEVER`, `INCONCLUSIVE`, or `PROCEED_CANDIDATE`. This
is a pre-scoring plumbing abort and can support only plumbing conclusions.

## Retained evidence

Raw canary records remain outside tracked source in the enrollment-designated
mode-`0700` directory. The tracked record publishes digests rather than model
content:

- `canaries.jsonl` SHA-256: `289df2e9d952aa770deefc4ec5fbf8aa4376d84a20dfe81bc1eac3428c8d2740`
- `run.lock.json` SHA-256: `8ccae88aca30db481a00a4198589e421ea985dc635bdacfab5d750c11477120f`
- lock status: `aborted_before_scored_calls`

The raw directory path is intentionally represented portably in the enrollment
and is not a repository artifact.

## Postmortem probe disclosure

After the abort, one additional **canary-only, non-enrolled postmortem probe**
used `terminal-review-canary`, the `constraint_set` arm, repetition `0`, and
sample seed `151191272`. It preserved the enrolled model, prompt, strict JSON
schema, 320-token cap, context limit, temperature, and timeout, but explicitly
set the provider request field `think=false`.

- Probe request digest: `d519b4b98f50bab4e40ec545da48a37c0d5dafe1deeeebf3b35125e7070bf3e9`
- Provider result: `done_reason=stop`, `eval_count=126`
- Answer-channel content: 443 characters
- Thinking-channel content: 0 characters
- Mechanical parser result: schema valid, no parse error

The probe did produce outcome-bearing answer content. That content was not
printed, persisted, scored, inspected by the analyst, or compared with the
answer key; only the metadata above was returned. Consequently no response
digest is available. The probe is quarantined from every estimate and from any
future cohort's canary or scored records.

## Interpretation and next boundary

The installed model advertises a `thinking` capability. The uniform
`done_reason=length`, exact 320-token consumption, and empty answer channel
show that cohort v0 left the provider's thinking mode underspecified. Strict
parsing and exclusion of thinking text behaved correctly; the request contract
did not.

Governed review `bd74c50504b0f79a` accepted a new cohort only under these
boundaries: publish and preserve this abort; freeze and test an exact
`think=false` request; keep the model digest, prompts, scenarios, answer keys,
renderers, scorer, thresholds, seeds, token cap, timeout, and no-retry policy
unchanged; commit and push a separate enrollment with a new empty output
directory before generation; and repeat the complete fail-closed canary gate.
Any permanent runtime, schema, tool, or dashboard integration remains out of
scope and requires a separate review.
