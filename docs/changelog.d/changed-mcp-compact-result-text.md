- **`/mcp/` tool results are compact JSON text** (no API change). The MCP
  transport parsed each handler's JSON into a dict and returned it to
  FastMCP, which rendered it back to text with `indent=2`. Every `/mcp/`
  result therefore reached the agent's context indented, one array element
  per line, while REST (`/v1/tools/call`) already answered compactly. The
  registered tools now hand FastMCP one text block rendered by the same
  serializer with the same fallback and no indent
  (`src/tool_registration.py`, `_mcp_wire_result`), so the text differs from
  before only in whitespace outside strings and parses to the same value.
  Nothing else changes on the wire: every tool registers with
  `structured_output=False`, so no tool advertises an outputSchema and no
  result carries structuredContent, on mcp 1.x or 2.x; the text block was
  and remains the whole result. The non-JSON fallback
  (`{"success": true, "text": ...}`) and the wrapper's error envelope keep
  their shape. `get_tool_wrapper` still returns the dict that `use_tool`'s
  nested invoker reads. Measured basis: over 14 days of one deployment's
  local Claude Code transcripts, indentation was 13.5% of governance
  tool-result bytes (about 19.5 KB a day), 11.7% since 2026-09-26, and
  largest on `describe_tool` (23%), `list_tools` (20%) and
  `check_working_state` (15%). Tokens saved are fewer than bytes, because
  runs of spaces tokenize cheaply. The SDK's check-in and metrics readers
  are pinned to resolve the verdict, coherence, risk and E/I/S/V from the
  compact text (`tests/test_mcp_text_compact.py`).
