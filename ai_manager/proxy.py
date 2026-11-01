"""Real client address and scheme behind a local tunnel or proxy (cloudflared, Tailscale Serve).

The forwarding headers are only believed when the direct TCP peer is a trusted proxy (loopback by default);
anything else could forge them. uvicorn runs with proxy_headers=False so request.client stays the direct peer.
"""

import ipaddress

from fastapi import Request


def parse_trusted(entries) -> tuple:
    """IP networks, plus exact names (the test client's peer is the literal string "testclient")."""
    parsed = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        try:
            parsed.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            parsed.append(entry)
    return tuple(parsed)


def _address(value):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def is_trusted(peer, trusted) -> bool:
    address = _address(peer)
    return any(
        (address is not None and item.version == address.version and address in item)
        if not isinstance(item, str)
        else item == peer
        for item in trusted
    )


def client_ip(request: Request, trusted) -> str:
    peer = request.client.host if request.client else "unknown"
    if not is_trusted(peer, trusted):
        return peer
    # Cloudflare overwrites CF-Connecting-IP at its edge, so the client cannot choose it.
    if address := _address(request.headers.get("cf-connecting-ip")):
        return str(address)
    # X-Forwarded-For: each proxy appends the address it saw, so walk from the right past trusted hops.
    # Several header lines count as one list (the right-most entry is the nearest hop).
    for part in reversed(",".join(request.headers.getlist("x-forwarded-for")).split(",")):
        address = _address(part)
        if address is None:
            break
        if not is_trusted(str(address), trusted):
            return str(address)
    return peer


def is_https(request: Request, trusted) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    if not is_trusted(peer, trusted):
        return False
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def on_listener(request: Request, port: int) -> bool:
    """True when the connection was accepted on the manager's own listener (the local port uvicorn serves)."""
    server = request.scope.get("server")
    return bool(server) and len(server) > 1 and server[1] == port


def enrollment_address(request: Request, trusted, port: int):
    """The tailnet-relevant client address for /api/enroll/report, or None when it cannot be believed.

    Forwarded headers count only when the direct peer is a trusted proxy (loopback, for Tailscale Serve) and the request
    came in on the manager's own listener; the address is the right-most untrusted X-Forwarded-For hop. CF-Connecting-IP
    is never believed here (a tailnet client could send it through Serve). Tailscale-User-* headers are never read."""
    if not on_listener(request, port) or request.headers.get("cf-connecting-ip") or request.headers.get("cf-ray"):
        return None
    return client_ip(request, trusted)
