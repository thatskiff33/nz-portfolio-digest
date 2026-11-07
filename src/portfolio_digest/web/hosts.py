"""Trusted Host checks, including bracketed IPv6 literals with optional ports."""

from ipaddress import IPv6Address

from starlette.middleware.trustedhost import TrustedHostMiddleware


class DashboardTrustedHostMiddleware(TrustedHostMiddleware):
    async def __call__(self, scope, receive, send):
        # Starlette splits Host at the first colon, which cannot match IPv6.
        if scope["type"] in {"http", "websocket"}:
            hosts = [value.decode("latin-1") for name, value in scope["headers"] if name == b"host"]
            if len(hosts) == 1 and hosts[0].startswith("["):
                literal, separator, suffix = hosts[0][1:].partition("]")
                try:
                    address = IPv6Address(literal)
                except ValueError:
                    pass
                else:
                    valid_port = not suffix or (
                        suffix.startswith(":")
                        and len(suffix) <= 6
                        and suffix[1:].isascii()
                        and suffix[1:].isdigit()
                        and 0 < int(suffix[1:]) <= 65535
                    )
                    if separator and valid_port and str(address) in self.allowed_hosts:
                        await self.app(scope, receive, send)
                        return
        await super().__call__(scope, receive, send)
