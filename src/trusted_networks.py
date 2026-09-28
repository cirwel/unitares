"""The networks this server treats as the operator's own.

One definition, two readers: the REST and dashboard WebSocket access checks
(``src/http_routes/access.py``) trust a caller whose peer address is in this
set, and the local model endpoint classifier (``src/local_inference_env.py``)
calls an endpoint ``local`` when its IP literal is in it. Keeping both on this
module means "local" cannot mean two different things in one server.

Stdlib only: the agent processes import ``src.local_inference_env``, which
imports this, and neither may pull in the web stack.
"""

from __future__ import annotations

import ipaddress
import logging
import os

logger = logging.getLogger(__name__)

# Built-in set: loopback and the private RFC 1918 ranges. A Docker Compose
# install reaches the server through the bridge gateway, which is in
# 172.16.0.0/12. Overlay or VPN ranges are not built in: 100.64.0.0/10 (the
# CGNAT range that Tailscale assigns from, and that some ISPs use for their own
# subscribers) was, which trusted one operator's network layout on every
# install. An operator on such a network lists it in UNITARES_TRUSTED_NETWORKS.
BUILTIN_TRUSTED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
)

_extra_networks_cache: tuple[str, tuple] = ("", ())


def extra_trusted_networks() -> tuple:
    """Networks the operator adds to the built-in trusted set (UNITARES_TRUSTED_NETWORKS).

    Comma-separated CIDRs or addresses, for example ``100.64.0.0/10`` for a
    Tailscale tailnet. Unset or empty adds nothing. An entry that does not
    parse, including a CIDR with host bits set (``203.0.113.7/8``, a likely
    typo for one host), is logged and skipped, never widened into something
    broader. A catch-all (``0.0.0.0/0``, ``::/0``) is honoured, since the
    operator wrote it, but logged, because it trusts every caller.
    """
    global _extra_networks_cache
    raw = os.getenv("UNITARES_TRUSTED_NETWORKS", "").strip()
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
        if net.prefixlen == 0:
            logger.warning(
                "UNITARES_TRUSTED_NETWORKS: %s trusts every caller; local-posture "
                "auth is effectively off",
                net,
            )
        nets.append(net)
    _extra_networks_cache = (raw, tuple(nets))
    return _extra_networks_cache[1]


def is_trusted_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``addr`` is in the built-in set or an operator-listed network."""
    return any(addr in net for net in (*BUILTIN_TRUSTED_NETWORKS, *extra_trusted_networks()))
