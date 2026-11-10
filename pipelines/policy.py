"""Outbound policy: which provider URLs may be fetched, and DNS pinned to public IPv4.

Cloud Run has no outbound host allowlist, so this module is the only guard against
requests to other hosts or internal addresses (SSRF).
"""
import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.util import connection
from urllib3.connectionpool import HTTPSConnectionPool

from . import config
from .errors import PipelineError


def _deny(reason: str, **detail) -> PipelineError:
    return PipelineError(403, "endpoint_not_allowed", "This provider endpoint is not allowed.",
                         {"reason": reason, **detail})


def _norm(host) -> str:
    return (host or "").strip().lower().rstrip(".")


def check_url(url: str, provider_host: str) -> None:
    """HTTPS, port 443, no userinfo, no encoded slashes, /public path, allowlisted host equal to the provider."""
    raw = (url or "").strip()
    if any(s in raw.lower() for s in ("%2f", "%5c", "\\")):
        raise _deny("encoded_slash")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _deny("malformed_url")
    if parts.scheme.lower() != "https":
        raise _deny("not_https")
    if parts.username is not None or parts.password is not None:
        raise _deny("userinfo")
    if port not in (None, 443):
        raise _deny("custom_port")
    host = _norm(parts.hostname)
    if host not in config.ALLOWED_PROVIDER_HOSTS:
        raise _deny("host_not_allowed", host=host)
    if host != provider_host:
        raise _deny("host_not_counterparty", host=host)
    if not (parts.path == "/public" or parts.path.startswith("/public/")):
        raise _deny("not_public_path")


def check_endpoint(endpoint: str, counter_party_address: str) -> str:
    """Validate the EDR endpoint against the counterparty; returns the provider host."""
    try:
        provider_host = _norm(urlsplit((counter_party_address or "").strip()).hostname)
    except ValueError:
        provider_host = ""
    if not provider_host:
        raise _deny("counterparty_invalid")
    check_url(endpoint, provider_host)
    return provider_host


def resolve_public_ipv4(host: str) -> str:
    """Resolve once; every address must be public IPv4. The returned IP is the one connected to."""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        raise PipelineError(502, "upstream_error", "Data provider not reachable. Try again.", {"error": "dns"})
    addrs = sorted({info[4][0] for info in infos})
    if not addrs or any(not ipaddress.ip_address(a).is_global for a in addrs):
        raise _deny("non_public_address", host=host)
    return addrs[0]


class _PinnedHTTPSConnection(HTTPSConnection):
    """Opens the socket to the validated IP. self.host stays the hostname, so the Host header,
    SNI and certificate check all use the hostname (urllib3 derives self.host from _dns_host,
    so _dns_host must not be overwritten)."""

    def _new_conn(self):
        host = _norm(self.host)
        if host not in config.ALLOWED_PROVIDER_HOSTS:
            raise _deny("host_not_allowed", host=host)
        ip = resolve_public_ipv4(host)
        try:
            return connection.create_connection(
                (ip, self.port), self.timeout,
                source_address=self.source_address, socket_options=self.socket_options,
            )
        except socket.timeout as e:
            raise ConnectTimeoutError(self, f"Connection to {host} timed out.") from e
        except OSError as e:
            raise NewConnectionError(self, f"Failed to establish a new connection: {e}") from e


class _PinnedPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"https": _PinnedPool}


def pinned_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # no proxies from the environment: they would bypass pinning
    session.mount("https://", _PinnedAdapter())
    session.mount("http://", _PinnedAdapter())  # http has no pool class: fails closed
    return session
