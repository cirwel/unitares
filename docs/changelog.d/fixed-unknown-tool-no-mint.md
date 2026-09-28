- **an unknown tool name no longer mints an identity before it is refused:**
  the dispatch pipeline ran its pre-steps, identity resolution among them,
  before it looked the tool up. Identity resolution treats a name it cannot
  classify as identity-required, which is the fail-closed default, and on a
  session miss outside strict identity it takes the auto-mint retry
  (`spawn_reason="dispatch_auto_mint"`). A REST or `use_tool` caller that
  guessed a name with no handler therefore got a fresh, persisted identity
  and then `TOOL_NOT_FOUND`, one ghost per call. `run_tool_dispatch_pipeline`
  now refuses a name that neither a handler nor an alias answers before any
  pre-step runs, so the refusal leaves no identity behind. Names a handler or alias does answer are unaffected
  and run the same steps in the same order. MCP clients were mostly spared,
  because a name missing from `tools/list` is refused before dispatch; the
  path mattered more once the 24 unadvertised aliases were removed, since
  names such as `status` and `hello` are plausible first-call guesses.
