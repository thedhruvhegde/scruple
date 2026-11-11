"""Outbound HTTP to addresses a tenant chose, without server-side request forgery.

A webhook endpoint, a Jira site and a Slack response URL are URLs a tenant (or Slack)
supplies, and the request goes out from inside the deployment. `SafeHttp` refuses
anything that could reach the deployment itself or its network:

- scheme allowlist: `https` only (`allow_http=True` for local development);
- no credentials in the URL, no fragments, ports only 443 or 1024-65535 (and 80 for
  http), hostnames of letters, digits, dots and hyphens;
- every address the name resolves to must be globally routable: private (RFC 1918,
  fc00::/7), loopback, link-local (169.254/16 and fe80::/10, which includes cloud
  metadata at 169.254.169.254), carrier-grade NAT (100.64/10), multicast, reserved,
  unspecified and IPv4-mapped IPv6 addresses are refused (`ipaddress.is_global`, plus
  explicit checks);
- the request is pinned to the address that was checked: the URL is rewritten to that
  IP, `Host` keeps the name, and TLS uses the name for SNI and certificate checks
  (httpcore's `sni_hostname` extension). A DNS answer that changes between the check
  and the connection (rebinding) cannot redirect it;
- redirects are never followed; timeouts are short; at most `max_response_bytes` of a
  response are read.

`allow_hosts` exempts exact hostnames from the address check only (for an internal
receiver the operator trusts); the scheme and URL rules still apply.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

Resolver = Callable[[str, int], list[str]]
_HOST = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
)
MAX_URL = 2_048
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UnsafeURLError(ValueError):
    """The URL or the address it resolves to is not allowed."""


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"{host} does not resolve: {exc.strerror}") from None
    return sorted({str(info[4][0]) for info in infos})


def address_problem(addr: str) -> str | None:
    """Why `addr` may not be contacted, or None when it is a public address."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return f"{addr!r} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return f"{ip} is an IPv4-mapped address"
    checks = (
        ("loopback", ip.is_loopback),
        ("private", ip.is_private),
        ("link-local", ip.is_link_local),
        ("multicast", ip.is_multicast),
        ("reserved", ip.is_reserved),
        ("unspecified", ip.is_unspecified),
        ("carrier-grade NAT", isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT),
    )
    for name, hit in checks:
        if hit:
            return f"{ip} is a {name} address"
    if not ip.is_global:
        return f"{ip} is not a public address"
    return None


@dataclass(frozen=True)
class Target:
    url: str  # as given
    scheme: str
    host: str
    port: int
    pinned_url: str  # the URL with the checked IP in place of the name
    address: str


def check_url(
    url: str,
    *,
    allow_http: bool = False,
    resolver: Resolver = system_resolver,
    allow_hosts: Collection[str] = (),
) -> Target:
    """Validate `url` and resolve it to one checked address. Raises UnsafeURLError."""
    if len(url) > MAX_URL:
        raise UnsafeURLError("URL is too long")
    try:
        parts = urlsplit(url)
        port_given = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {exc}") from None
    scheme = parts.scheme.lower()
    allowed = ("https", "http") if allow_http else ("https",)
    if scheme not in allowed:
        raise UnsafeURLError(f"scheme {scheme or '(none)'!r} is not allowed (only {allowed})")
    if parts.username or parts.password or "@" in parts.netloc:
        raise UnsafeURLError("credentials in the URL are not allowed")
    if parts.fragment:
        raise UnsafeURLError("fragments are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    literal = _literal_ip(host)
    if literal is None and not _HOST.match(host):
        raise UnsafeURLError(f"host {host!r} is not a valid hostname")
    port = port_given or (443 if scheme == "https" else 80)
    if not (port == 443 or (scheme == "http" and port == 80) or 1024 <= port <= 65535):
        raise UnsafeURLError(f"port {port} is not allowed")
    addresses = [literal] if literal is not None else resolver(host, port)
    if not addresses:
        raise UnsafeURLError(f"{host} has no addresses")
    if host not in allow_hosts:
        for addr in addresses:
            problem = address_problem(addr)
            if problem:
                raise UnsafeURLError(f"{host} resolves to a blocked address: {problem}")
    address = addresses[0]
    shown = f"[{address}]" if ":" in address else address
    netloc = f"{shown}:{port_given}" if port_given else shown
    pinned = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    return Target(url, scheme, host, port, pinned, address)


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


@dataclass(frozen=True)
class Response:
    status_code: int
    headers: Mapping[str, str]
    body: bytes  # at most max_response_bytes

    def json(self) -> Any:
        import json

        return json.loads(self.body)


@dataclass
class SafeHttp:
    """POST/GET with the checks above. `transport` is injectable (tests never touch
    the network: pass an `httpx.MockTransport`)."""

    transport: httpx.BaseTransport | None = None
    resolver: Resolver = system_resolver
    allow_http: bool = False
    allow_hosts: Collection[str] = ()
    timeout_s: float = 10.0
    max_response_bytes: int = 64 * 1024
    user_agent: str = "scruple-integrations/1"
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                transport=self.transport,
                timeout=self.timeout_s,
                follow_redirects=False,
                trust_env=False,  # an inherited proxy would bypass the address check
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> Response:
        target = check_url(
            url, allow_http=self.allow_http, resolver=self.resolver, allow_hosts=self.allow_hosts
        )
        sent = {"User-Agent": self.user_agent, **(headers or {})}
        sent["Host"] = target.host if target.port in (80, 443) else f"{target.host}:{target.port}"
        extensions: dict[str, Any] = {}
        if target.scheme == "https":
            extensions["sni_hostname"] = target.host
        req = self._http().build_request(
            method, target.pinned_url, headers=sent, content=content, extensions=extensions
        )
        resp = self._http().send(req, stream=True)
        try:
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) >= self.max_response_bytes:
                    del body[self.max_response_bytes :]
                    break
            return Response(resp.status_code, dict(resp.headers), bytes(body))
        finally:
            resp.close()

    def post(self, url: str, **kw: Any) -> Response:
        return self.request("POST", url, **kw)

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)


__all__ = [
    "Resolver",
    "Response",
    "SafeHttp",
    "Target",
    "UnsafeURLError",
    "address_problem",
    "check_url",
    "system_resolver",
]
