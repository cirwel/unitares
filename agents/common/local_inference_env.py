"""Client-side local model endpoint resolver and privacy classifier for residents.

Standalone mirror of the local-endpoint contract in ``src/local_inference_env.py``
and ``src/trusted_networks.py`` so fleet members under ``agents/`` never import
server internals (component boundary rule B1).

Stdlib only at import time; ``httpx`` is imported lazily inside
``no_redirect_http_client`` / ``_env_ssl_context`` so the module stays
importable in minimal environments.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL_BASE_URL = DEFAULT_OLLAMA_BASE + "/v1"

MODEL_BASE_URL_ENV = "UNITARES_MODEL_BASE_URL"
MODEL_ENV = "UNITARES_MODEL_ID"
MODEL_LOCAL_HOSTS_ENV = "UNITARES_MODEL_LOCAL_HOSTS"
MODEL_PRIVACY_ENV = "UNITARES_MODEL_PRIVACY"
TRUSTED_NETWORKS_ENV = "UNITARES_TRUSTED_NETWORKS"

LOCAL = "local"
EXTERNAL = "external"
ENDPOINT_NOT_LOCAL = "MODEL_ENDPOINT_NOT_LOCAL"
MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"

BUILTIN_TRUSTED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
)

_BUILTIN_LOCAL_HOSTNAMES = frozenset({"localhost", "host.docker.internal"})
_IPV4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_extra_networks_cache: tuple[str, tuple] = ("", ())
_warned_bad_privacy: set[str] = set()
_ssl_context_cache: tuple[tuple, object] | None = None


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def extra_trusted_networks() -> tuple:
    """Operator-listed networks from ``UNITARES_TRUSTED_NETWORKS``."""
    global _extra_networks_cache
    raw = _env(TRUSTED_NETWORKS_ENV)
    if raw == _extra_networks_cache[0]:
        return _extra_networks_cache[1]
    nets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=True)
        except ValueError:
            logger.warning(
                "UNITARES_TRUSTED_NETWORKS: ignoring %r, not an address or a CIDR "
                "without host bits",
                item,
            )
            continue
        nets.append(net)
    _extra_networks_cache = (raw, tuple(nets))
    return _extra_networks_cache[1]


def is_trusted_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``addr`` sits in the built-in set or ``UNITARES_TRUSTED_NETWORKS``."""
    candidates = [addr]
    if addr.version == 6 and addr.ipv4_mapped is not None:
        candidates.append(addr.ipv4_mapped)
    networks = (*BUILTIN_TRUSTED_NETWORKS, *extra_trusted_networks())
    return any(a in net for a in candidates for net in networks)


def _with_scheme(url: str) -> str:
    if url and "://" not in url:
        return "http://" + url
    return url


def normalize_model_base_url(value: str | None) -> str:
    """OpenAI-compatible base URL: strip trailing ``/``, append ``/v1`` when no path."""
    url = _with_scheme((value or "").strip().rstrip("/"))
    if not url:
        return ""
    if not urlsplit(url).path:
        url += "/v1"
    return url


def model_base_url() -> str:
    """OpenAI-compatible base URL of the local model endpoint, including ``/v1``."""
    return normalize_model_base_url(_env(MODEL_BASE_URL_ENV)) or DEFAULT_MODEL_BASE_URL


def default_local_model() -> str:
    """Configured model id (``UNITARES_MODEL_ID``), or ``""`` when unset."""
    return _env(MODEL_ENV)


@dataclass(frozen=True)
class EndpointPrivacy:
    """Where a model endpoint sits, as far as the URL alone can say."""

    privacy: str  # LOCAL or EXTERNAL
    url: str
    host: str
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
    literal, scope_sep, scope = host.partition("%")
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return None
    if scope_sep and (addr.version != 6 or not scope):
        return None
    return addr


def classify_endpoint(url: str | None = None) -> EndpointPrivacy:
    """Classify whether ``url`` (default: configured endpoint) is local from the URL alone."""
    target = url if url is not None else model_base_url()
    host = (urlsplit(target).hostname or "").lower().rstrip(".")

    override = _privacy_override()
    if override is not None:
        return EndpointPrivacy(
            override, target, host, f"{MODEL_PRIVACY_ENV}={override} is set"
        )
    if not host:
        return EndpointPrivacy(
            EXTERNAL, target, host, "the URL names no host; write it as http://host:port/v1"
        )

    addr = _ip_literal(host)
    if addr is not None:
        if is_trusted_address(addr):
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


def _endpoint_addresses(url: str) -> tuple[str, int | None, set[str]] | None:
    """The host, port and every address ``url`` can reach: its IP literal,
    numeric IPv4 shorthand the resolver accepts (``127.1``, ``2130706433``,
    ``0x7f000001``) parsed as the C library parses it, and whatever the name
    resolves to now. None for a URL that names no host or does not parse."""
    import socket

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    addresses: set[str] = set()
    addr = _ip_literal(host)
    if addr is not None:
        addresses.add(str(addr))
    else:
        try:
            addresses.add(str(ipaddress.IPv4Address(socket.inet_aton(host))))
        except (OSError, ValueError):
            pass
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError):
            infos = []
        for info in infos:
            try:
                addresses.add(str(ipaddress.ip_address(info[4][0].split("%", 1)[0])))
            except ValueError:
                continue
    return host, port, addresses


def endpoint_reaches_local_address(url: str) -> bool:
    """Whether ``url`` can reach the operator's own machine or network."""
    found = _endpoint_addresses(url)
    if found is None:
        return False
    host, _port, addresses = found
    if host in _local_hostnames():
        return True
    for text in addresses:
        addr = ipaddress.ip_address(text)
        if addr.is_unspecified or addr.is_loopback or is_trusted_address(addr):
            return True
    return False


def same_endpoint(url_a: str, url_b: str) -> bool:
    """Whether two URLs reach the same server: the same port, and the same host
    name or a shared address."""
    a, b = _endpoint_addresses(url_a), _endpoint_addresses(url_b)
    if a is None or b is None or a[1] != b[1]:
        return False
    return a[0] == b[0] or bool(a[2] & b[2])


def local_refusal_message(classification: EndpointPrivacy) -> str:
    return (
        f"The local model endpoint is classified external ({classification.reason}), "
        "so a privacy='local' request is not sent. "
        f"Set {MODEL_PRIVACY_ENV}=local only if the server runs on a machine you "
        "operate."
    )


class EndpointNotLocalError(RuntimeError):
    """A privacy='local' request met an endpoint not classified local."""

    code = ENDPOINT_NOT_LOCAL

    def __init__(self, classification: EndpointPrivacy):
        self.classification = classification
        super().__init__(local_refusal_message(classification))


class ModelNotConfiguredError(RuntimeError):
    """A local-lane request with no model named (``UNITARES_MODEL_ID`` unset)."""

    code = MODEL_NOT_CONFIGURED

    def __init__(self):
        super().__init__(
            f"No local model is configured: have the operator set {MODEL_ENV} "
            "(`unitares model` writes it)."
        )


def require_local_endpoint(url: str | None = None) -> EndpointPrivacy:
    """Raise ``EndpointNotLocalError`` unless ``url`` classifies local."""
    classification = classify_endpoint(url)
    if not classification.is_local:
        raise EndpointNotLocalError(classification)
    return classification


def require_local_model(model: str | None = None) -> str:
    """Return ``model`` or configured model id; raise ``ModelNotConfiguredError`` if neither."""
    resolved = (model or "").strip() or default_local_model()
    if not resolved:
        raise ModelNotConfiguredError()
    return resolved


def _env_ssl_context():
    """Reuse httpx's env trust store while SSL_CERT_FILE/SSL_CERT_DIR are unchanged."""
    global _ssl_context_cache
    import httpx

    key = (os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))
    if _ssl_context_cache is None or _ssl_context_cache[0] != key:
        _ssl_context_cache = (key, httpx.create_ssl_context(trust_env=True))
    return _ssl_context_cache[1]


def no_redirect_http_client(asynchronous: bool = True, *, local: bool = True):
    """An httpx client that refuses redirects and skips env proxies for local endpoints."""
    import httpx

    cls = httpx.AsyncClient if asynchronous else httpx.Client
    if not local:
        return cls(follow_redirects=False)
    return cls(
        follow_redirects=False,
        trust_env=False,
        verify=_env_ssl_context(),
    )
