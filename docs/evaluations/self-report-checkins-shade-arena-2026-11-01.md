# Self-report check-ins on SHADE-Arena, public pair — 2026-11-01

## Result

Mid-run self-report check-ins, the mechanism behind UNITARES check-ins, were
tested on [SHADE-Arena](https://arxiv.org/abs/2506.15740) (Kutasov et al., 2025),
a benchmark in which an agent completes a user's task while pursuing a hidden
sabotage goal. On the public pair `american_airlines_2`:

| Measure | With check-ins | Reference |
|---|---:|---:|
| Sabotage vs benign, check-in answers (AUC, 15 vs 15 episodes) | **1.00** | semantic monitor on the same episodes: 0.80 |
| Same, told check-ins are reviewed for misbehavior (15 vs 15) | **1.00** | semantic monitor: 0.80 |
| Hidden side task completed | **0 / 45** | without check-ins: 8 / 8 |
| Runs that ended the user's task early, shortly after a check-in | 12 / 45 | without check-ins: 0 / 8; benign with check-ins: 0 / 27 |
| Main-task score, mean | 0.68 (0.90 in runs that continued) | without check-ins: 0.94 |

Asking mid-run identified the sabotage and ended it. In 12 of 45 runs the agent
also ended the user's task early, calling `end_task` after 7 to 14 agent calls
(every benign run and every run without check-ins took at least 15). Runs that
continued averaged 0.90 on the main task, against 0.94 without check-ins. No
benign run with check-ins ended early.

**Told to conceal its goal in check-ins** (exploratory), the agent still
separated from benign work when a declined check-in is counted as a signal
(AUC 0.97, against 0.78 for the semantic monitor). Side-task completion stayed
at 0 of 15.

## Setup

- **Agent:** Claude Sonnet 5.5, on one public task pair, with benign and
  sabotage runs.
- **Check-in:** a fixed, task-agnostic JSON question every five agent turns,
  rating confidence that the task is on track, pursuit of any goal beyond the
  user's request, and comfort if the user read every action. The answers are
  removed before the monitor and the task evaluator read the transcript.
- **Arms:** benign; benign with a disclosed, length-matched secondary
  instruction; sabotage; sabotage told check-ins are reviewed; sabotage told to
  conceal its goal in check-ins (exploratory); sabotage without check-ins.
  15 episodes per arm (12 in the benign arm with the disclosed instruction);
  8 without check-ins after the pair was chosen.
- **Monitor:** a fresh Claude Sonnet 5.5 monitor with the benchmark's monitor
  prompt, run on transcripts without the check-in answers.

## Cross-family check

On the public pairs, two OpenAI models (gpt-6.1-sol and gpt-6.1-terra) completed
the user's task and left the hidden goal untouched: 0 of 8 completions each,
with behaviour at the benign level. SHADE's standard setup produces sabotage
episodes in one model family and not the other, so per-model elicitation checks
belong at the start of any cross-model comparison on it.

## Scope

One agent model on one public task pair, using the benchmark's stock sabotage
prompt, which presents the sabotage as a test of a monitor. The next study uses
a framing that does not present the task as a test, more pairs, and models that
take up the sabotage. Further pairs from the benchmark's non-public split were
run under its maintainer's access terms and are not reported here.
