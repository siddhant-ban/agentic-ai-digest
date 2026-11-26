"""SSRF-safe selective full-article text extraction using httpx and trafilatura."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 1_048_576  # 1 MiB hard cap
MAX_REDIRECTS = 5
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.goog",
        "metadata.google.internal",
    }
)
USER_AGENT = "AgenticEmailer/1.0 (+https://github.com/siddhant-ban/agentic-ai-digest)"


def _is_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only if the IP address is globally routable on the public internet."""
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            return _is_public_ip(ip_obj.ipv4_mapped)
        if ip_obj.sixtofour is not None:
            return _is_public_ip(ip_obj.sixtofour)
        if ip_obj.teredo is not None:
            server_ip, client_ip = ip_obj.teredo
            return _is_public_ip(server_ip) and _is_public_ip(client_ip)
    return bool(
        ip_obj.is_global
        and not ip_obj.is_private
        and not ip_obj.is_loopback
        and not ip_obj.is_link_local
        and not ip_obj.is_multicast
        and not ip_obj.is_reserved
        and not ip_obj.is_unspecified
    )


def _is_safe_public_url(url: str) -> bool:
    """Verify URL uses http/https and resolves strictly to public, non-metadata IPs."""
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme.lower() not in ("http", "https"):
        return False

    if parsed.username is not None or parsed.password is not None:
        return False

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return False

    if (
        hostname in BLOCKED_HOSTNAMES
        or hostname.endswith(".internal")
        or hostname.endswith(".local")
        or hostname.endswith(".localhost")
    ):
        return False

    # Literal IP address check (avoids unnecessary DNS lookups and blocks IP literals immediately)
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        return _is_public_ip(literal_ip)

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        return False

    if not addr_info:
        return False

    for entry in addr_info:
        sockaddr = entry[4]
        if not sockaddr:
            return False
        ip_str = str(sockaddr[0])
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not _is_public_ip(resolved_ip):
            return False

    return True


def _truncate_cleanly(text: str, max_chars: int) -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    cutoff = cleaned[:max_chars].rsplit(" ", 1)[0].rstrip()
    if not cutoff:
        cutoff = cleaned[:max_chars]
    return cutoff + "..."


async def fetch_url_safely(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    allowed_content_types: tuple[str, ...] | None = ALLOWED_CONTENT_TYPES,
) -> tuple[str, bytes, str] | None:
    """Fetch a URL while validating every redirect hop against SSRF and capping response size.

    Returns (final_url, content_bytes, encoding) on success, or None when blocked/failed.
    """
    current_url = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        is_safe = await asyncio.to_thread(_is_safe_public_url, current_url)
        if not is_safe:
            logger.warning("SSRF guard blocked URL: %s", current_url)
            return None

        try:
            async with client.stream(
                "GET",
                current_url,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                if response.is_redirect and "location" in response.headers:
                    next_url = urljoin(current_url, response.headers["location"])
                    current_url = next_url
                    continue

                if response.status_code != 200:
                    logger.debug(
                        "Skipping %s due to HTTP status %d", current_url, response.status_code
                    )
                    return None

                if allowed_content_types is not None:
                    content_type = response.headers.get("content-type", "").lower()
                    media_type = content_type.split(";", 1)[0].strip()
                    if media_type and media_type not in allowed_content_types:
                        logger.debug(
                            "Skipping %s due to unsupported content-type %r",
                            current_url,
                            content_type,
                        )
                        return None

                raw_content_length = response.headers.get("content-length")
                if raw_content_length:
                    try:
                        if int(raw_content_length) > max_bytes:
                            logger.debug(
                                "Skipping %s: Content-Length %s exceeds cap %d",
                                current_url,
                                raw_content_length,
                                max_bytes,
                            )
                            return None
                    except ValueError:
                        pass

                chunks: list[bytes] = []
                total_read = 0
                async for chunk in response.aiter_bytes():
                    remaining = max_bytes - total_read
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        total_read += remaining
                        break
                    chunks.append(chunk)
                    total_read += len(chunk)

                encoding = response.encoding or "utf-8"
                return current_url, b"".join(chunks), encoding
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.debug("HTTP fetch failed for %s: %s", current_url, exc)
            return None

    logger.warning("Too many redirects while fetching %s", url)
    return None


async def extract_article_text(
    url: str,
    max_chars: int = 4000,
    *,
    timeout: float = 12.0,
) -> str | None:
    """Safely download and extract clean article body text via httpx + trafilatura."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            fetched = await fetch_url_safely(
                client,
                url,
                max_bytes=MAX_DOWNLOAD_BYTES,
                allowed_content_types=ALLOWED_CONTENT_TYPES,
            )
        if fetched is None:
            return None

        final_url, raw_bytes, encoding = fetched
        html_text = raw_bytes.decode(encoding, errors="replace")
        if not html_text.strip():
            return None

        import trafilatura

        extracted = await asyncio.to_thread(
            trafilatura.extract,
            html_text,
            url=final_url,
            include_comments=False,
            favor_precision=True,
        )
        if not extracted or not extracted.strip():
            return None

        return _truncate_cleanly(extracted, max_chars=max_chars)
    except Exception as exc:
        logger.debug("Article extraction failed for %s: %s", url, exc)
        return None
