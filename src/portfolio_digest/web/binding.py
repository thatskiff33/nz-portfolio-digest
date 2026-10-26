"""Refuse public/wildcard host bindings, including accidental Compose overrides."""

from ipaddress import ip_address, ip_network


def private_host(value: str) -> str:
    try:
        address = ip_address(value)
    except ValueError:
        raise ValueError("DASHBOARD_BIND must be a loopback or Tailscale IP address.") from None
    tailscale = (
        ip_network("100.64.0.0/10") if address.version == 4 else ip_network("fd7a:115c:a1e0::/48")
    )
    if not address.is_loopback and address not in tailscale:
        raise ValueError("DASHBOARD_BIND must be a loopback or Tailscale IP address.")
    return value
