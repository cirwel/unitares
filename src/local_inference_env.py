"""Where the local model lives: one resolver for every local-Ollama reader.

The server (``call_model``/consult, the in-process dialectic reviewer, knowledge
synthesis) and the agent processes (the orchestrated dialectic reviewer,
``agents/local_resident``) all resolve the host and default model here, so one
setting moves every reader:

- ``UNITARES_OLLAMA_BASE`` is the documented name: the Ollama root URL, without
  ``/v1`` (for example ``http://host.docker.internal:11434``).
- ``UNITARES_OLLAMA_BASE_URL`` stays accepted as an alias, in either form.
- A trailing ``/`` or ``/v1`` is removed from either value, so each caller gets
  the form it needs: the root (``ollama_base_url``) for the native ``/api/chat``
  route and the socket probe, or root + ``/v1`` (``ollama_openai_base_url``)
  for an OpenAI-compatible client.
- An empty value counts as unset. A compose file that passes ``${VAR:-}``
  through therefore keeps the default instead of producing a model of ``""`` or
  a base URL of ``/v1``.

Stdlib only and outside ``src/mcp_handlers``: the agent processes import this,
and importing anything under ``src.mcp_handlers`` loads the whole handler
package.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE = "http://localhost:11434"
DEFAULT_LOCAL_MODEL = "gemma4:latest"

# (canonical, alias) pairs already warned about, so a per-call resolver does not
# repeat the same warning on every inference call.
_warned_disagreements: set[tuple[str, str]] = set()


def normalize_ollama_base(value: str | None) -> str:
    """Reduce an Ollama URL to its root: no surrounding space, no trailing ``/``
    and no trailing ``/v1``. Returns ``""`` for an empty or missing value."""
    url = (value or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url


def _alias_ollama_base() -> str:
    """Accepted alias of UNITARES_OLLAMA_BASE, used when that is unset; with or without /v1."""
    return normalize_ollama_base(os.getenv("UNITARES_OLLAMA_BASE_URL", ""))


def ollama_base_url() -> str:
    """Root URL of the local Ollama endpoint, without /v1; default http://localhost:11434.

    ``UNITARES_OLLAMA_BASE`` wins; when it is unset or empty, the alias
    ``UNITARES_OLLAMA_BASE_URL`` is used; when both are unset, localhost.
    """
    canonical = normalize_ollama_base(os.getenv("UNITARES_OLLAMA_BASE", ""))
    alias = _alias_ollama_base()
    if canonical and alias and canonical != alias:
        pair = (canonical, alias)
        if pair not in _warned_disagreements:
            _warned_disagreements.add(pair)
            logger.warning(
                "UNITARES_OLLAMA_BASE (%s) and UNITARES_OLLAMA_BASE_URL (%s) "
                "disagree; using UNITARES_OLLAMA_BASE. Set one of them.",
                canonical,
                alias,
            )
    return canonical or alias or DEFAULT_OLLAMA_BASE


def ollama_openai_base_url() -> str:
    """The OpenAI-compatible endpoint of the same Ollama: ``ollama_base_url()`` + ``/v1``."""
    return ollama_base_url() + "/v1"


def default_local_model() -> str:
    """Default model for local inference (UNITARES_LLM_MODEL override)."""
    return os.getenv("UNITARES_LLM_MODEL", "gemma4:latest").strip() or DEFAULT_LOCAL_MODEL
