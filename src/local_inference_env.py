"""Where the local model lives: one resolver for every local-model reader.

The server (``call_model``/consult, the in-process dialectic reviewer, knowledge
synthesis, check-in enrichments) and the agent processes (the orchestrated
dialectic reviewer's ``local`` backend, ``agents/local_resident``) all resolve
the endpoint and model here, so one setting moves every reader.

Settings (the documented names):

- ``UNITARES_MODEL_BASE_URL``: the OpenAI-compatible base URL of the model
  server, including ``/v1`` (``http://localhost:11434/v1`` for Ollama on this
  machine, which is the default). A value with no path at all gets ``/v1``
  added, so a bare Ollama root still works.
- ``UNITARES_MODEL_ID``: the model id the endpoint serves. Default
  ``gemma4:latest`` for now; a later release removes the default.
- ``UNITARES_MODEL_LOCAL_HOSTS`` and ``UNITARES_MODEL_PRIVACY``: see
  ``classify_endpoint`` below.

Callers get the form they need: ``model_base_url()`` for an OpenAI-compatible
client, and ``ollama_base_url()`` (the base without its trailing ``/v1``) for
Ollama's native routes and the reachability probe.

Older names are read through ``SETTING_ALIASES`` until the release each entry
names; ``tests/test_local_inference_env.py`` fails once ``VERSION`` reaches it.
An empty value counts as unset, so a compose file that passes ``${VAR:-}``
through keeps the default instead of producing a model of ``""``.

Stdlib only and outside ``src/mcp_handlers``: the agent processes import this,
and importing anything under ``src.mcp_handlers`` loads the whole handler
package.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.trusted_networks import is_trusted_address

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL_BASE_URL = DEFAULT_OLLAMA_BASE + "/v1"
DEFAULT_LOCAL_MODEL = "gemma4:latest"

MODEL_BASE_URL_ENV = "UNITARES_MODEL_BASE_URL"
MODEL_ENV = "UNITARES_MODEL_ID"
MODEL_LOCAL_HOSTS_ENV = "UNITARES_MODEL_LOCAL_HOSTS"
MODEL_PRIVACY_ENV = "UNITARES_MODEL_PRIVACY"
TRUSTED_NETWORKS_ENV = "UNITARES_TRUSTED_NETWORKS"

# Every setting the local model lane reads. tests/test_local_inference_env.py
# checks that each one reaches every process that reads it: mapped for
# governance-mcp in docker-compose.yml, present in the LaunchAgent template, and
# forwarded to the orchestrated reviewer. A setting that exists but never
# arrives makes an install look configured when it is not.
LOCAL_MODEL_SETTINGS: tuple[str, ...] = (
    MODEL_BASE_URL_ENV,
    MODEL_ENV,
    MODEL_LOCAL_HOSTS_ENV,
    MODEL_PRIVACY_ENV,
    # Shared with the REST access checks (src/trusted_networks.py); the
    # classifier reads it for an IP-literal endpoint on, say, a tailnet peer.
    TRUSTED_NETWORKS_ENV,
)


@dataclass(frozen=True)
class SettingAlias:
    """An older setting name still read in place of ``new`` until ``removed_in``."""

    old: str
    new: str
    removed_in: str


# The only place an old name is read. Each entry is removed by the release it
# names (two minor releases after the one that renamed it); a test fails once
# VERSION reaches that release, so the release cut deletes the row or a
# reviewed diff moves its date. Order matters within one new name: an earlier
# row wins over a later one when both are set.
SETTING_ALIASES: tuple[SettingAlias, ...] = (
    SettingAlias("UNITARES_OLLAMA_BASE", MODEL_BASE_URL_ENV, "3.2.0"),
    SettingAlias("UNITARES_OLLAMA_BASE_URL", MODEL_BASE_URL_ENV, "3.2.0"),
    SettingAlias("UNITARES_LLM_MODEL", MODEL_ENV, "3.2.0"),
)

# (winning name, its value, other name, its value) disagreements already
# warned about, so a per-call resolver does not repeat the same warning on
# every inference call.
_warned_disagreements: set[tuple[str, str, str, str]] = set()


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def aliases_for(new: str) -> tuple[SettingAlias, ...]:
    return tuple(a for a in SETTING_ALIASES if a.new == new)


def old_names_in_use() -> list[SettingAlias]:
    """Alias rows whose old name is set (non-empty) in this process's environment."""
    return [a for a in SETTING_ALIASES if _env(a.old)]


def _resolve(new: str, normalize) -> str:
    """Value of ``new``, else of its aliases in table order; ``""`` when none is set.

    Only a disagreement is logged, once per pair of values: an old name set on
    its own is quiet for as long as its table row lives.
    """
    names = [new] + [a.old for a in aliases_for(new)]
    candidates = [(name, normalize(name, _env(name))) for name in names]
    present = [(name, value) for name, value in candidates if value]
    if not present:
        return ""
    winner_name, winner = present[0]
    for other_name, other in present[1:]:
        if other != winner:
            key = (winner_name, winner, other_name, other)
            if key not in _warned_disagreements:
                _warned_disagreements.add(key)
                logger.warning(
                    "%s (%s) and %s (%s) disagree; using %s. Set one of them.",
                    winner_name, winner, other_name, other, winner_name,
                )
    return winner


def normalize_ollama_base(value: str | None) -> str:
    """Reduce a model server URL to its root: no surrounding space, no trailing
    ``/`` and no trailing ``/v1``. Returns ``""`` for an empty or missing value."""
    url = (value or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url


def normalize_model_base_url(value: str | None) -> str:
    """An OpenAI-compatible base URL: trailing ``/`` removed, and ``/v1`` added
    only when the URL has no path at all (an Ollama root such as
    ``http://gpu:11434``). A base with its own path is kept as given."""
    url = (value or "").strip().rstrip("/")
    if not url:
        return ""
    if not urlsplit(url).path:
        url += "/v1"
    return url


def _normalize_base_setting(name: str, value: str) -> str:
    """The new name is already an OpenAI-compatible base; the older names are
    Ollama roots (``/v1`` optional), so they get exactly one ``/v1``."""
    if name == MODEL_BASE_URL_ENV:
        return normalize_model_base_url(value)
    root = normalize_ollama_base(value)
    return root + "/v1" if root else ""


def model_base_url() -> str:
    """OpenAI-compatible base URL of the local model endpoint, including ``/v1``.

    ``UNITARES_MODEL_BASE_URL``, else an alias from ``SETTING_ALIASES`` (the
    older Ollama root names, with or without ``/v1``), else
    ``http://localhost:11434/v1``.
    """
    return _resolve(MODEL_BASE_URL_ENV, _normalize_base_setting) or DEFAULT_MODEL_BASE_URL


def _alias_ollama_base() -> str:
    """``UNITARES_OLLAMA_BASE_URL`` reduced to its root; nothing here calls it.

    Kept only because master gained it in #2495 on 2026-09-26 and the fleet
    push guard treats removing a symbol that new as a likely rebase revert.
    ``SETTING_ALIASES`` is the real alias path. Delete after 2026-10-26.
    """
    return normalize_ollama_base(os.getenv("UNITARES_OLLAMA_BASE_URL", ""))


def ollama_base_url() -> str:
    """The endpoint's root: ``model_base_url()`` without its trailing ``/v1``.

    Ollama's native routes (``/api/chat``, ``/api/version``) and the
    reachability probe live here. Default ``http://localhost:11434``.
    """
    return normalize_ollama_base(model_base_url())


def ollama_openai_base_url() -> str:
    """Kept for callers that predate ``model_base_url()``; the same value."""
    return model_base_url()


def default_local_model() -> str:
    """Model for local inference: ``UNITARES_MODEL_ID``, else its alias, else gemma4:latest."""
    return _resolve(MODEL_ENV, lambda _name, v: v.strip()) or DEFAULT_LOCAL_MODEL


# ---------------------------------------------------------------------------
# Privacy class of the endpoint, from the URL alone
# ---------------------------------------------------------------------------

LOCAL = "local"
EXTERNAL = "external"
ENDPOINT_NOT_LOCAL = "MODEL_ENDPOINT_NOT_LOCAL"

_BUILTIN_LOCAL_HOSTNAMES = frozenset({"localhost", "host.docker.internal"})
# RFC 4193 unique local addresses: private by definition, like RFC 1918.
_UNIQUE_LOCAL_V6 = ipaddress.ip_network("fc00::/7")
_warned_bad_privacy: set[str] = set()


@dataclass(frozen=True)
class EndpointPrivacy:
    """Where a model endpoint sits, as far as the URL alone can say."""

    privacy: str  # LOCAL or EXTERNAL
    url: str
    host: str
    # The setting that would change the answer, named in a refusal.
    reason: str

    @property
    def is_local(self) -> bool:
        return self.privacy == LOCAL


def _local_hostnames() -> frozenset[str]:
    listed = {
        h.strip().lower().rstrip(".")
        for h in _env(MODEL_LOCAL_HOSTS_ENV).split(",")
        if h.strip()
    }
    return _BUILTIN_LOCAL_HOSTNAMES | listed


def _privacy_override() -> str | None:
    raw = _env(MODEL_PRIVACY_ENV).lower()
    if raw in (LOCAL, EXTERNAL):
        return raw
    if raw and raw not in _warned_bad_privacy:
        _warned_bad_privacy.add(raw)
        logger.warning(
            "%s=%r is not 'local' or 'external'; ignoring it and classifying the "
            "endpoint from its URL.",
            MODEL_PRIVACY_ENV,
            raw,
        )
    return None


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def classify_endpoint(url: str | None = None) -> EndpointPrivacy:
    """Whether the model endpoint at ``url`` (default: the configured one) is local.

    Decided from the URL only, never from a DNS answer: a name classified from
    one private answer could connect to a public one a moment later, and a
    ``privacy='local'`` prompt would leave while the server reported otherwise.

    - ``UNITARES_MODEL_PRIVACY=local|external`` wins, for an operator whose own
      server sits on a public address (or who wants every call treated as
      leaving).
    - An IP literal is local when it is in the server's trusted networks
      (loopback, RFC 1918, and ``UNITARES_TRUSTED_NETWORKS``; see
      ``src/trusted_networks.py``) or is an RFC 4193 address.
    - A hostname is local only when it is ``localhost``,
      ``host.docker.internal`` or listed in ``UNITARES_MODEL_LOCAL_HOSTS``
      (comma-separated). Every other name is external, even one that resolves
      to a private address today.
    """
    target = url if url is not None else model_base_url()
    host = (urlsplit(target).hostname or "").lower().rstrip(".")

    override = _privacy_override()
    if override is not None:
        return EndpointPrivacy(
            override, target, host, f"{MODEL_PRIVACY_ENV}={override} is set"
        )
    if not host:
        return EndpointPrivacy(EXTERNAL, target, host, "the URL names no host")

    addr = _ip_literal(host)
    if addr is not None:
        local = is_trusted_address(addr) or (
            isinstance(addr, ipaddress.IPv6Address) and addr in _UNIQUE_LOCAL_V6
        )
        if local:
            return EndpointPrivacy(LOCAL, target, host, "address in the trusted networks")
        return EndpointPrivacy(
            EXTERNAL,
            target,
            host,
            f"{host} is not in the trusted networks; add its network to "
            "UNITARES_TRUSTED_NETWORKS if the server is yours",
        )

    if host in _local_hostnames():
        return EndpointPrivacy(LOCAL, target, host, "hostname listed as local")
    return EndpointPrivacy(
        EXTERNAL,
        target,
        host,
        f"hostname {host} is not listed as local; add it to "
        f"{MODEL_LOCAL_HOSTS_ENV} if the server is yours",
    )


def no_redirect_http_client(asynchronous: bool = True, *, local: bool = True):
    """An httpx client for the OpenAI SDK that keeps a prompt where it was sent.

    - **No redirects.** The SDK follows redirects by default and re-sends the
      POST, prompt included, to the new location. The endpoint was classified
      from its own URL, so a 307 or 308 to another host would carry a
      local-privacy prompt somewhere nothing checked. An OpenAI-compatible
      server has no reason to redirect a completion call; the redirect comes
      back as an error instead.
    - **No environment proxy for a local endpoint** (``local=True``). httpx
      honours ``HTTP_PROXY``/``HTTPS_PROXY`` by default, which would send a
      prompt for ``localhost`` through whatever proxy the environment names.
      An external endpoint (``local=False``) keeps the environment's proxy,
      which an operator may need to reach it.

    httpx is imported here so this module stays importable without it.
    """
    import httpx

    cls = httpx.AsyncClient if asynchronous else httpx.Client
    return cls(follow_redirects=False, trust_env=not local)


def direct_urlopen(request, *, timeout: float):
    """``urllib.request.urlopen`` without environment proxies, for a local
    endpoint's native routes (``/api/chat``, ``/api/version``): urllib, like
    httpx, would otherwise route through ``HTTP_PROXY``."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


class EndpointNotLocalError(RuntimeError):
    """A privacy='local' request met an endpoint the server does not call local."""

    code = ENDPOINT_NOT_LOCAL

    def __init__(self, classification: EndpointPrivacy):
        self.classification = classification
        super().__init__(local_refusal_message(classification))


def local_refusal_message(classification: EndpointPrivacy) -> str:
    # The host only, never the full URL: a base URL can carry a path or
    # userinfo that does not belong in an agent-facing error.
    return (
        f"The local model endpoint is classified external ({classification.reason}), "
        "so a privacy='local' request is not sent. "
        f"Set {MODEL_PRIVACY_ENV}=local only if the server runs on a machine you "
        "operate."
    )


def require_local_endpoint(url: str | None = None) -> EndpointPrivacy:
    """Raise ``EndpointNotLocalError`` unless the endpoint classifies local.

    Makes no network call, so a refusal happens before anything is sent.
    """
    classification = classify_endpoint(url)
    if not classification.is_local:
        raise EndpointNotLocalError(classification)
    return classification


# ---------------------------------------------------------------------------
# Is the endpoint an Ollama? (gates Ollama-only routes such as /api/chat)
# ---------------------------------------------------------------------------

# Same budget and cache lifetime as the registry's reachability probe: a local
# endpoint answers in well under 0.5 s, and a 5 s cache bounds how often a busy
# server re-asks. Blocking; async callers run it with asyncio.to_thread.
LOCAL_PROBE_TIMEOUT_S = 0.5
_OLLAMA_DETECT_TTL_S = 5.0
_ollama_detect_cache: dict[str, tuple[float, bool]] = {}
_ollama_detect_lock = threading.Lock()


def _probe_ollama_version(root: str, timeout: float) -> bool | None:
    """True/False for an answer that says Ollama or not; None for no answer."""
    try:
        with direct_urlopen(root + "/api/version", timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError:
        return False  # the server answered, without the route
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return isinstance(payload, dict) and isinstance(payload.get("version"), str)


def is_ollama_endpoint(root: str | None = None, *, timeout: float = LOCAL_PROBE_TIMEOUT_S) -> bool:
    """True when ``GET {root}/api/version`` answers like Ollama. Cached for 5 s.

    ``root`` defaults to the configured endpoint's root. A server that answers
    without the route, or with another body, is not Ollama: the caller then
    uses the OpenAI-compatible route every server offers. No answer within
    ``timeout`` keeps this root's previous result (a busy Ollama can miss a
    0.5 s budget without having stopped being Ollama), or reads as "not
    Ollama" when there is none.
    """
    root = normalize_ollama_base(root) if root is not None else ollama_base_url()
    now = time.monotonic()
    with _ollama_detect_lock:
        cached = _ollama_detect_cache.get(root)
        if cached is not None and (now - cached[0]) < _OLLAMA_DETECT_TTL_S:
            return cached[1]
    answer = _probe_ollama_version(root, timeout)
    result = answer if answer is not None else bool(cached and cached[1])
    with _ollama_detect_lock:
        _ollama_detect_cache[root] = (time.monotonic(), result)
    return result
