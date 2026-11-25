"""Outbound URL validation against SSRF / DNS rebinding (build.md §3.1M, §24.5).

Resolve-then-validate every address; callers must connect to the returned IPs
(pinning) and must not follow redirects without re-validating.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "100::/64",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]


class UnsafeDestination(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SafeDestination:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _BLOCKED_NETS)


async def validate_outbound_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"https"}),
    allowed_hosts: frozenset[str] | None = None,
    allow_private: bool = False,
) -> SafeDestination:
    parts = urlsplit(url)
    if parts.scheme not in allowed_schemes:
        raise UnsafeDestination("URL scheme not allowed")
    if parts.username or parts.password:
        raise UnsafeDestination("Credentials in URL are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeDestination("URL host missing")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise UnsafeDestination("Host is not on the allowlist")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeDestination("Host could not be resolved") from exc
    addresses = tuple(sorted({str(info[4][0]) for info in infos}))
    if not addresses:
        raise UnsafeDestination("Host resolved to no addresses")
    if not allow_private and any(is_blocked_ip(a) for a in addresses):
        raise UnsafeDestination("Destination resolves to a private, loopback, link-local or reserved address")
    return SafeDestination(url=url, host=host, port=port, addresses=addresses)
