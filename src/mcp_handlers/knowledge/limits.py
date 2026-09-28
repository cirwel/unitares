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

# The details value a knowledge update may store. resolution_notes are appended
# to details as a timestamped block, so each closing note grows the field and
# nothing else stops it: on AGE, details is a graph-node property interpolated
# into Cypher, where GraphMixin._sanitize_cypher_param refuses a string over
# 128 KiB, and that refusal fails the whole update, status included, reported
# as "Discovery not found". The handler measures the value it would store (the
# stored details or the details the call sends, earlier notes included, plus
# the new block) and refuses the update when it is over this bound. It sits
# above MAX_DETAILS_LEN, so a finding stored at that cap still has room for
# notes, and well below the Cypher limit. Characters, as both of those are.
MAX_UPDATED_DETAILS_LEN = 96 * 1024

# The summary a knowledge update may store. A store keeps at most
# MAX_SUMMARY_LEN characters and appends SUMMARY_TRUNCATION_MARKER when it cuts
# a longer summary, so the update bound admits that marker: a stored summary
# sent back unchanged is accepted. An update refuses a longer summary rather
# than cutting it, since it replaces the caller's text. On AGE the summary is a
# graph-node property interpolated into Cypher, where
# GraphMixin._sanitize_cypher_param refuses a string over 128 KiB, and that
# refusal fails the whole update, reported as "Discovery not found".
SUMMARY_TRUNCATION_MARKER = "..."
MAX_UPDATED_SUMMARY_LEN = MAX_SUMMARY_LEN + len(SUMMARY_TRUNCATION_MARKER)

# Tags are short labels for search, filters and rollups, not a place for text.
# Counted and measured after normalize_tags, which is what storage writes. On
# AGE an update sends the whole list to Cypher as one JSON string, and a store
# sends each tag as its own parameter (in the node's list and as the name of its
# Tag node) and runs one MERGE per tag inside its transaction. A string over the
# 128 KiB Cypher parameter limit fails the write. Both bounds sit far below it.
MAX_TAGS = 50
MAX_TAG_LEN = 128

# The metadata a store writes on the AGE discovery node: related_files, the
# provenance the call supplies (memory_context, task_label and the other
# provenance fields, or a batch item's provenance object), the response_to
# link, related_to and confidence. It is one Cypher parameter, which
# GraphMixin._sanitize_cypher_param serializes with json.dumps and refuses over
# 128 KiB, failing the whole store. Measured the same way, on the node as the
# handler would store it.
MAX_DISCOVERY_METADATA_LEN = 32 * 1024
