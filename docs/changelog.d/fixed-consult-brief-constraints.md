- `consult` now tells the model to apply the brief's length, format and scope
  constraints to its answer content. Critique no longer treats every brief as a
  proposal or requires unrequested sections. An exact-brief local replay reproduced
  the original two-sentence failure in three old-wrapper runs and returned two
  sentences in three revised-wrapper runs; this is bounded format evidence, not
  a general guarantee. Host terminal-answer validation remains strict.
