"""Bounded, allowlisted fetch of issuer filing PDFs.

Only official exchange archive hosts are fetchable, over https, with no
redirects, public-IP DNS resolution, a browser User-Agent (the NSE archive
rejects bare script agents), bounded time and bytes, a PDF content-type and
magic-bytes check. Arbitrary user/model-supplied URLs are refused. The
fetched PDF's text is untrusted data downstream - never instructions.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

ALLOWED_HOSTS = {"nsearchives.nseindia.com", "www.bseindia.com"}
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_BYTES = 25_000_000


class FilingFetchError(RuntimeError):
    pass


def _public_ips(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FilingFetchError(f"DNS failed for {host}") from exc
    ips = sorted({i[4][0] for i in infos})
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if not addr.is_global:
            raise FilingFetchError(f"{host} resolves to non-public address {ip}")
    return ips


def check_filing_url(url: str) -> str:
    """Validate a candidate filing URL; returns the host. Raises otherwise."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise FilingFetchError(f"only https filings are fetched: {url}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise FilingFetchError(f"host not in filing allowlist: {host}")
    _public_ips(host)
    return host


def fetch_filing_pdf(url: str, dest: Path | str, *, timeout: float = 30.0,
                     max_bytes: int = MAX_BYTES, http=None) -> Path:
    check_filing_url(url)
    dest = Path(dest)
    client = http or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=False)
    with client.stream("GET", url, timeout=timeout) as resp:
        if 300 <= resp.status_code < 400:
            raise FilingFetchError(f"redirect refused for {url}")
        if resp.status_code != 200:
            raise FilingFetchError(f"HTTP {resp.status_code} for {url}")
        ctype = resp.headers.get("content-type", "")
        if "pdf" not in ctype.lower():
            raise FilingFetchError(f"not a PDF content-type ({ctype}) for {url}")
        chunks, total = [], 0
        for chunk in resp.iter_bytes(65536):
            total += len(chunk)
            if total > max_bytes:
                raise FilingFetchError(f"filing exceeds {max_bytes} bytes")
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(b"%PDF-"):
        raise FilingFetchError(f"magic-bytes check failed for {url}")
    dest.write_bytes(data)
    return dest
