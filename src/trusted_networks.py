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
# A dual-stack socket reports an IPv4 peer as ::ffff:a.b.c.d.
_IPV4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")


def extra_trusted_networks() -> tuple:
    """Networks the operator adds to the built-in trusted set (UNITARES_TRUSTED_NETWORKS).

    Comma-separated CIDRs or addresses, for example ``100.64.0.0/10`` for a
    Tailscale tailnet. Unset or empty adds nothing. An entry that does not
    parse, including a CIDR with host bits set (``203.0.113.7/8``, a likely
    typo for one host), is logged and skipped, never widened into something
    broader. A catch-all is honoured, since the operator wrote it, but logged,
    because it trusts every caller: ``0.0.0.0/0`` or ``::/0``, entries that
    together cover a whole address family (``0.0.0.0/1,128.0.0.0/1``), or an
    IPv6 range holding ``::ffff:0:0/96``, which a dual-stack bind reports every
    IPv4 caller from.
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
        nets.append(net)
    # Judge coverage on the collapsed union, so split entries count as one. An
    # IPv4-mapped IPv6 entry trusts the IPv4 callers it maps (is_trusted_address
    # matches both forms), so it counts toward the IPv4 union as well.
    mapped_v4 = [
        ipaddress.ip_network((int(n.network_address) & 0xFFFFFFFF, n.prefixlen - 96))
        for n in nets
        if n.version == 6 and n.subnet_of(_IPV4_MAPPED)
    ]
    # The built-in networks are trusted too, so they count toward the union:
    # listing everything outside 10.0.0.0/8 trusts every caller. They never
    # cover a family on their own, so an empty setting still logs nothing.
    for version in (4, 6):
        listed = [n for n in (*BUILTIN_TRUSTED_NETWORKS, *nets) if n.version == version]
        if version == 4:
            listed += mapped_v4
        for net in ipaddress.collapse_addresses(listed):
            if net.prefixlen == 0:
                logger.warning(
                    "UNITARES_TRUSTED_NETWORKS: %s trusts every caller; local-posture "
                    "auth is effectively off",
                    net,
                )
            elif version == 6 and _IPV4_MAPPED.subnet_of(net):
                logger.warning(
                    "UNITARES_TRUSTED_NETWORKS: %s trusts every caller over IPv4 on a "
                    "dual-stack bind (::ffff:0:0/96); local-posture auth is "
                    "effectively off",
                    net,
                )
    _extra_networks_cache = (raw, tuple(nets))
    return _extra_networks_cache[1]


def is_trusted_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``addr`` is in the built-in set or an operator-listed network."""
    # A dual-stack bind reports an IPv4 peer as ::ffff:a.b.c.d. Match the IPv4
    # address it carries against the IPv4 networks too, or a listed IPv4 range
    # (and the built-in ones) would never match on such a socket.
    candidates = [addr]
    if addr.version == 6 and addr.ipv4_mapped is not None:
        candidates.append(addr.ipv4_mapped)
    networks = (*BUILTIN_TRUSTED_NETWORKS, *extra_trusted_networks())
    return any(a in net for a in candidates for net in networks)
