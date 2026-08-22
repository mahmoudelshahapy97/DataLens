"""Working out who a request actually came from.

``X-Forwarded-For`` is written by whoever sent the request. Behind a reverse proxy
it is the only way to see the real client; reachable directly it is a free-text
field an attacker controls. The original code read it unconditionally::

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()

That is the *left*-most entry -- the one the client itself supplies -- so per-IP
login throttling was defeated by a single header, and the API port was published
straight to the host.

The rule implemented here is the standard one: walk the chain from the right,
discarding hops that are in ``VANNA_TRUSTED_PROXIES``, and take the first address
that is not. Consider the header at all only when the immediate peer is itself a
trusted proxy. With no trusted proxies configured the header is ignored entirely.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger("vanna.net")


def _parse(value: str) -> Optional[Any]:
    try:
        # A forwarded entry may carry a port, and IPv6 arrives bracketed.
        text = value.strip()
        if text.startswith("["):
            text = text[1: text.find("]")] if "]" in text else text[1:]
        elif text.count(":") == 1:
            text = text.split(":")[0]
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _trusted(address: Any, networks: Sequence[Any]) -> bool:
    return any(address in network for network in networks)


def client_ip(
    peer: Optional[str],
    forwarded_for: str,
    trusted_proxies: Tuple[Any, ...],
) -> str:
    """The address to attribute this request to.

    Args:
        peer: The socket peer -- ``request.client.host``.
        forwarded_for: The raw ``X-Forwarded-For`` header, possibly empty.
        trusted_proxies: Networks whose entries in the chain may be believed.

    Returns:
        An address, or ``""`` when none can be determined. Callers key rate limits
        on this, so an empty result must be treated as a single shared bucket
        rather than as "no limit".
    """
    peer_address = _parse(peer or "")

    # Nothing configured, or the immediate peer is not a proxy we trust: the only
    # thing we know is who actually connected.
    if not trusted_proxies or peer_address is None or not _trusted(peer_address, trusted_proxies):
        return str(peer_address) if peer_address else (peer or "")

    hops = [_parse(part) for part in forwarded_for.split(",") if part.strip()]
    for address in reversed(hops):
        if address is None:
            # An unparseable entry means the chain cannot be trusted past this
            # point. Stop here rather than skipping it, which would let a client
            # hide behind a deliberately malformed hop.
            break
        if not _trusted(address, trusted_proxies):
            return str(address)

    # Every hop was a trusted proxy, or the header was absent: the peer is the
    # closest thing to a client we have.
    return str(peer_address)


def request_ip(request: Any, trusted_proxies: Tuple[Any, ...]) -> str:
    """``client_ip`` for a Starlette/FastAPI request."""
    return client_ip(
        request.client.host if request.client else "",
        request.headers.get("x-forwarded-for", ""),
        trusted_proxies,
    )
