"""Length limits for knowledge-graph content.

These control three distinct concerns:

- `MAX_SUMMARY_LEN` / `MAX_DETAILS_LEN`: write-side caps applied when a
  discovery is stored. Content beyond the cap is truncated with an ellipsis
  and a `_truncated` marker on the response so agents see the loss.

- `EMBED_DETAILS_WINDOW`: how many characters of `details` are concatenated
  onto `summary` to form the text that gets embedded for semantic search.
  This is the number that actually determines what the vector DB "sees".
  Raising write caps without raising this does not improve retrieval.
  Sized to fit inside BGE-M3's 8192-token budget with headroom.

- `DETAILS_PREVIEW_CHARS`: how much of `details` is included alongside the
  summary when a search returns without `include_details=True`. Gives the
  agent enough context to decide whether to fetch the full discovery.
"""

MAX_SUMMARY_LEN = 4000
# Store medium-sized source documents intact. Read-side pagination and the
# independent embedding window below keep this larger persistence envelope
# from expanding normal response or semantic-search context.
MAX_DETAILS_LEN = 64 * 1024
EMBED_DETAILS_WINDOW = 6000
DETAILS_PREVIEW_CHARS = 500

# closure_evidence is a handful of named statements or pointers (what shipped,
# what was observed), not a place for logs. It is stored as JSON text: in the
# jsonb column and, on AGE, as a graph-node property interpolated into Cypher,
# where GraphMixin._sanitize_cypher_param refuses any string over 128 KiB. That
# refusal fails the whole update, status included, and the handler reports it as
# "Discovery not found". The bound sits far below that limit and below
# MAX_DETAILS_LEN; longer material belongs in resolution_notes, which is appended
# to details. Measured on closure_evidence_to_json's output, the text storage
# writes (ASCII, so characters and bytes agree).
MAX_CLOSURE_EVIDENCE_BYTES = 8 * 1024
