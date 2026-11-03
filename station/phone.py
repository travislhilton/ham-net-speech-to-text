"""Addresses a phone can open the dashboard at, each with a QR code (generated here, offline).

The station's firewall rule admits the PC's own subnet and Tailscale, so those are the addresses offered:
the address the browser is already using (when it is not localhost), then each private-network IPv4 of this
PC, then its Tailscale address. Link-local and loopback addresses are never offered.
"""
from __future__ import annotations

import ipaddress
import socket

TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def kind(ip: str) -> str | None:
    """'tailscale' / 'network' for an address a phone could reach; None for loopback, link-local, public."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if a.version != 4 or a.is_loopback or a.is_link_local:
        return None
    if a in TAILSCALE:
        return "tailscale"
    return "network" if a.is_private else None


def local_ipv4() -> list[str]:
    ips: list[str] = []
    try:
        for fam, *_rest, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(sa[0])
    except OSError:
        pass
    try:                                             # the address of the default route (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9)); ips.append(s.getsockname()[0]); s.close()
    except OSError:
        pass
    return list(dict.fromkeys(ips))


def addresses(host_header: str | None, port: int, ips: list[str] | None = None) -> list[dict]:
    """[{url, label}] in the order a phone should try them."""
    out: list[dict] = []
    if host_header:
        h = host_header.rsplit(":", 1)[0].strip("[]")
        if h and h.lower() not in ("localhost", "127.0.0.1", "::1") and (kind(h) or not h[0].isdigit()):
            out.append({"url": f"http://{host_header}/", "label": "The address this browser is using"})
    for ip in (local_ipv4() if ips is None else ips):
        k = kind(ip)
        if k:
            out.append({"url": f"http://{ip}:{port}/",
                        "label": "Tailscale (phone has the Tailscale app on)" if k == "tailscale"
                        else "Same Wi-Fi or network as this PC"})
    seen, uniq = set(), []
    for a in out:
        if a["url"] not in seen:
            seen.add(a["url"]); uniq.append(a)
    return uniq


def qr_svg(text: str) -> str:
    import segno
    return segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
