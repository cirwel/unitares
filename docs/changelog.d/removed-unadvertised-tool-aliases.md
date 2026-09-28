- **24 unadvertised tool aliases** (compatibility: callable names removed).
  Names that only redirected a guessed or pre-consolidation name to a tool
  that keeps its canonical name now return `tool_not_found_error`. Its
  suggestions are fuzzy name matches and usually do not name the
  replacement, so the replacements are listed here: `status`, `my_status`, `check_status`, `metrics`, `state`
  (use `check_working_state` or `get_governance_metrics`); `start`, `init`,
  `register`, `login` (use `start_session` or `onboard`); `checkin`, `log`,
  `update` (use `sync_state` or `process_agent_update`); `authenticate`,
  `session`, `quick_start`, `recall_identity`, `bind_identity`, `hello`,
  `get_agent_api_key` (use `identity`); `request_exploration_session` (use
  `dialectic(action="get")`); `find_similar_discoveries_graph`,
  `get_related_discoveries_graph`, `get_response_chain_graph`,
  `reply_to_question` (use `knowledge` with the matching action). No
  capability is removed, the eight advertised workflow aliases are unchanged,
  and the advertised surface digest does not move. Hints that still named
  `get_agent_api_key` or `reply_to_question` now name `identity()` and
  `knowledge(action="store", response_to={...})`.
  The 38 other legacy aliases, such as `list_agents`, `observe_agent` and
  `store_knowledge_graph`, are unchanged: each shares its name with an
  internal handler, and several still have callers to repoint before they
  can go. The complexity validation error and the `complexity` field text
  now name `sync_state` as the alias that accepts named levels and scale
  objects.
