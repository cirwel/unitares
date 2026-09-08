"""Brief parameter descriptions for the advertised tool surface.

Every character of every parameter description is paid for on every
``tools/list``, in every session, by every client — before the agent has
decided it wants the tool at all. Measured 2026-09-08 on the FastMCP wire,
``sync_state`` cost 8,076 characters (~2,000 tokens) and the five-tool
``minimal`` profile cost ~5,470 tokens, roughly half of it parameter prose.
A profile cut does not touch that: it removes names from the list, not words
from the names that remain.

Pydantic also emits ``title`` annotations at schema nodes. They are stripped
by default, restorable with ``UNITARES_TOOL_SCHEMA_PROPERTY_TITLES=keep``.
The transformation preserves validation and caller data named ``title``, but
changes schema fingerprints. Catalog trimming alone is insufficient: FastMCP
regenerates titles while building its typed wrappers. ``tool_mode_listing``
applies this same policy to the final MCP listing without mutating validation
models. Measure that layer with ``tool_surface_cost.py --surface mcp``;
``--surface catalog`` measures the upstream definitions separately.

So this module trims the *advertised* text and leaves the authored text where
it already lives. ``describe_tool(tool_name=..., action=...)`` reads the
Pydantic models directly and still returns every word, which is the whole
point of the discovery seam added 2026-09-07: the wire orients, describe_tool
explains.

Trimming is deterministic, not lossy-by-guess:

- A field may carry an authored short form as ``json_schema_extra={"brief":
  ...}``. That always wins, and the ``brief`` key never reaches a caller — it
  is stripped from the wire and from describe_tool alike.
- Otherwise the first sentence is kept. Sentence detection skips abbreviations
  ("e.g.", "i.e.") and single-letter initials, and refuses a break so early
  that the result says nothing (``MIN_BRIEF``), in which case the next break
  is taken.
- A first sentence longer than the budget is cut on a word boundary and marked
  with an ellipsis, so a truncated description is visibly truncated.

Standard library only, and no project imports: this is shared by schema
construction, alias registration and the standalone tool-surface audit, which
runs in the repository's minimal development environment.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

#: Key a Pydantic ``Field(json_schema_extra=...)`` uses to author the short
#: form by hand. Stripped from every advertised surface.
BRIEF_KEY = "brief"

#: Hard cap on one advertised description, in characters. Beyond ~140 the
#: first-sentence rule is already doing the work (measured across the full
#: 42-tool surface: 140 -> 180 moves the lite profile by 8 tokens), so this is
#: a guard against a single runaway sentence, not the primary lever.
BRIEF_BUDGET = 140

#: Never break a sentence before this many characters — roughly four words.
#: Below that the "sentence" is punctuation inside a phrase far more often than
#: it is a description. Deliberately low: a first sentence that is short but
#: genuinely uninformative ("Required for action=store.") is fixed by authoring
#: a `brief`, not by making every field on the surface pay for a second
#: sentence it does not need (measured: raising this to 40 costs the minimal
#: profile 235 tokens).
MIN_BRIEF = 24

#: What the advertised schema does with authored field descriptions.
#:   brief — first sentence or authored short form (default)
#:   full  — the complete authored text, as before 2026-09-08
#:   off   — no field descriptions at all (the old STRIP_FIELD_DESCRIPTIONS)
FIELD_DESCRIPTION_MODES = ("brief", "full", "off")
DEFAULT_FIELD_DESCRIPTION_MODE = "brief"

#: What the advertised schema does with Pydantic-generated ``title`` keywords.
#:   strip — remove them (default); they echo the key and validate nothing
#:   keep  — leave them, as before 2026-09-08
PROPERTY_TITLE_MODES = ("strip", "keep")
DEFAULT_PROPERTY_TITLE_MODE = "strip"

_MODE_ENV = "UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS"
_TITLE_ENV = "UNITARES_TOOL_SCHEMA_PROPERTY_TITLES"
_LEGACY_STRIP_ENV = "UNITARES_TOOL_SCHEMA_STRIP_FIELD_DESCRIPTIONS"
_BUDGET_ENV = "UNITARES_TOOL_SCHEMA_BRIEF_BUDGET"

_TRUTHY = ("1", "true", "yes", "on")

# Words that end in a period without ending a sentence. Kept short and
# literal; a general abbreviation detector is not worth the failure mode of
# silently dropping half a description.
_ABBREVIATIONS = frozenset({
    "e.g.", "i.e.", "etc.", "cf.", "vs.", "approx.", "no.", "fig.", "ref.",
})

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_INITIAL = re.compile(r"^[A-Za-z]\.$")
# Leading punctuation must not hide an abbreviation: the last word of
# "The decision the agent took (e.g." is "(e.g.", which would otherwise miss
# the lookup and cut the description off mid-parenthesis.
_LEADING_PUNCT = '([{"\'`*_-'


def resolve_field_description_mode(mode: str | None = None) -> str:
    """Resolve the advertised field-description mode.

    An explicit argument wins. Otherwise ``UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS``
    is read, then the legacy boolean ``UNITARES_TOOL_SCHEMA_STRIP_FIELD_DESCRIPTIONS``
    (which predates this module and means ``off``). An unrecognized value falls
    back to the default and says so, rather than quietly serving a surface the
    operator did not ask for.
    """
    if mode is None:
        mode = os.getenv(_MODE_ENV, "").strip().lower()
    else:
        mode = str(mode).strip().lower()

    if not mode:
        if os.getenv(_LEGACY_STRIP_ENV, "0").strip().lower() in _TRUTHY:
            return "off"
        return DEFAULT_FIELD_DESCRIPTION_MODE

    if mode not in FIELD_DESCRIPTION_MODES:
        logger.warning(
            "%s=%r is not one of %s; serving %r",
            _MODE_ENV, mode, FIELD_DESCRIPTION_MODES, DEFAULT_FIELD_DESCRIPTION_MODE,
        )
        return DEFAULT_FIELD_DESCRIPTION_MODE
    return mode


def resolve_property_title_mode(mode: str | None = None) -> str:
    """Resolve what the advertised schema does with ``title`` keywords.

    An explicit argument wins, then ``UNITARES_TOOL_SCHEMA_PROPERTY_TITLES``.
    An unrecognized value falls back to the default and says so, rather than
    quietly serving a surface the operator did not ask for.
    """
    if mode is None:
        mode = os.getenv(_TITLE_ENV, "").strip().lower()
    else:
        mode = str(mode).strip().lower()

    if not mode:
        return DEFAULT_PROPERTY_TITLE_MODE
    if mode not in PROPERTY_TITLE_MODES:
        logger.warning(
            "%s=%r is not one of %s; serving %r",
            _TITLE_ENV, mode, PROPERTY_TITLE_MODES, DEFAULT_PROPERTY_TITLE_MODE,
        )
        return DEFAULT_PROPERTY_TITLE_MODE
    return mode


def resolve_brief_budget(budget: int | None = None) -> int:
    """Resolve the per-description character budget."""
    if budget is not None:
        return max(1, int(budget))
    raw = os.getenv(_BUDGET_ENV, "").strip()
    if not raw:
        return BRIEF_BUDGET
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("%s=%r is not an integer; serving %d", _BUDGET_ENV, raw, BRIEF_BUDGET)
        return BRIEF_BUDGET


def _sentence_break(text: str, minimum: int) -> int | None:
    """Index just past the first sentence-ending punctuation worth breaking on."""
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if end < minimum:
            continue
        word = text[:end].rsplit(" ", 1)[-1].lstrip(_LEADING_PUNCT)
        if word.lower() in _ABBREVIATIONS or _INITIAL.match(word):
            continue
        return end
    return None


def brief_text(
    text: str,
    *,
    budget: int = BRIEF_BUDGET,
    minimum: int = MIN_BRIEF,
) -> str:
    """The advertised short form of one authored description."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= budget:
        # Already inside the contract. Trimming here buys nothing the budget
        # has not already bought, and every sentence dropped from an
        # already-short description is one the caller must spend a
        # describe_tool call to get back.
        return collapsed

    cut = _sentence_break(collapsed, minimum)
    short = collapsed[:cut] if cut is not None else collapsed
    if len(short) <= budget:
        return short

    head = short[:budget]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + " …"


# JSON Schema keywords whose value is a *map of names to subschemas*. Their
# keys are caller-chosen parameter names, not keywords: `consult` really does
# take a parameter called `brief`, and a blind key-name walk deleted it from
# the advertised surface (caught by
# tests/test_tool_schema_validation.py::test_required_params_subset_of_properties).
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")

# Keywords whose value is caller data rather than a subschema. A default of
# ``{"description": "..."}`` is a value, not documentation, and must survive
# every mode untouched.
_DATA_KEYWORDS = ("default", "const", "enum", "examples", "example")


def apply_field_description_mode(
    node: Any,
    mode: str = DEFAULT_FIELD_DESCRIPTION_MODE,
    *,
    budget: int = BRIEF_BUDGET,
) -> Any:
    """Return a copy of a JSON Schema with its descriptions in ``mode``.

    Walks the schema structurally — subschema maps by value, data keywords not
    at all — so a nested model's own description is trimmed on the same rule as
    a top-level field's while a parameter that happens to be *named*
    ``description`` or ``brief`` is left alone.

    The ``brief`` authoring key is removed in every mode, ``full`` included, so
    no caller ever sees one parameter documented twice.
    """
    if isinstance(node, dict):
        override = node.get(BRIEF_KEY)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == BRIEF_KEY:
                continue
            if key == "description" and isinstance(value, str):
                if mode == "off":
                    continue
                if mode == "brief":
                    if isinstance(override, str) and override.strip():
                        # An authored short form is a deliberate choice and is
                        # not held to the budget: some parameters (an inline
                        # enum, a required-keys map) ARE their list.
                        out[key] = " ".join(override.split())
                    else:
                        out[key] = brief_text(value, budget=budget)
                else:
                    out[key] = value
                continue
            if key in _DATA_KEYWORDS:
                out[key] = copy.deepcopy(value)
                continue
            if key in _SUBSCHEMA_MAPS and isinstance(value, dict):
                out[key] = {
                    name: apply_field_description_mode(sub, mode, budget=budget)
                    for name, sub in value.items()
                }
                continue
            out[key] = apply_field_description_mode(value, mode, budget=budget)
        return out
    if isinstance(node, list):
        return [apply_field_description_mode(x, mode, budget=budget) for x in node]
    return node


def apply_property_title_mode(
    node: Any,
    mode: str = DEFAULT_PROPERTY_TITLE_MODE,
) -> Any:
    """Return a copy of a JSON Schema with ``title`` keywords in ``mode``.

    Walks structurally on exactly the rule
    :func:`apply_field_description_mode` uses, and for the same reason: under
    ``properties`` (and the other subschema maps) the keys are caller-chosen
    parameter names, so a parameter *named* ``title`` must survive while the
    ``title`` KEYWORD on a schema node is dropped. No tool ships such a
    parameter today; the walk is written so that adding one cannot silently
    delete it.

    ``mode="keep"`` returns ``node`` itself — not a copy — which is why the env
    flag can restore the pre-2026-09-08 surface without a second code path. The
    one production caller (``advertised_input_schema``) passes a schema that
    ``apply_field_description_mode`` has already copied, so nothing is aliased
    back to a Pydantic model; a new caller that means to mutate the result must
    copy it first.
    """
    if mode == "keep":
        return node
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title" and isinstance(value, str):
                continue
            if key in _DATA_KEYWORDS:
                out[key] = copy.deepcopy(value)
                continue
            if key in _SUBSCHEMA_MAPS and isinstance(value, dict):
                out[key] = {
                    name: apply_property_title_mode(sub, mode)
                    for name, sub in value.items()
                }
                continue
            out[key] = apply_property_title_mode(value, mode)
        return out
    if isinstance(node, list):
        return [apply_property_title_mode(x, mode) for x in node]
    return node


def drop_brief_keys(node: Any) -> Any:
    """Return a copy with the authoring key removed and descriptions intact."""
    return apply_field_description_mode(node, "full")
