- **MCP server instructions fit a 1,400-byte budget** (agent-facing text only;
  no tool, schema or digest changes). The `instructions` string every client
  receives at initialize was 3,438 UTF-8 bytes in progressive mode and 3,144
  in full mode. Fourteen days of one operator's local Claude Code and Codex
  transcripts showed Claude Code delivering only about the first 2,088 bytes
  (read from `mcp_instructions_delta.addedBlocks`), so every Claude Code
  session lost the tail: the discovery route (`list_tools(lite=true)`, then
  `describe_tool`, then `use_tool`) and the note that advertised parameter
  descriptions are abridged. The same transcripts showed the text arriving
  twice per session when an agent connects through both a local server and
  the hosted connector, about 297 KB a day of repeated orientation. The
  rewrite is 1,393 bytes (progressive) and 1,386 bytes (full) and keeps the
  product sentence, every positioning phrase the doc-drift lint pins, reading
  before binding, `start_session(force_new=true)` with `client_session_id`
  threading, declared lineage, the full list of pause exits, the four
  workflow names, the discovery route and the abridged note. Operator
  configuration (`UNITARES_TOOL_ADVERTISEMENT=full`, the ignored
  `GOVERNANCE_TOOL_MODE`) left the string, because an agent cannot set it; the
  interface contract's advertisement section documents it and now says the
  instructions string does not. Tests pin the byte budget in both modes and
  check that each required phrase ends inside it. The interface contract does
  not hash the instructions, so its version and surface digest do not move.
  This measures bytes delivered, not any change in agent behavior.
