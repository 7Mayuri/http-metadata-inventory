"""
services.py – Business logic layer.

Responsibilities
────────────────
• Fetch HTTP metadata (headers, cookies, page source) for a given URL using
  the `requests` library.
• Persist metadata to MongoDB via pymongo (upsert to avoid duplicates).
• Retrieve stored metadata by URL.
• Kick off background collection when a GET request finds no existing record.

All logic is kept *out* of the route handlers so it can be unit-tested
independently and re-used by both POST and GET flows.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from datetime import UTC, datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests as http_requests  # aliased to avoid shadowing FastAPI's Request
from pymongo.collection import Collection

from app.models import CookieItem, MetadataDocument

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "10"))

# A realistic User-Agent avoids being blocked by simple bot-detection.
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ──────────────────────────────────────────────
# SSRF PROTECTION
# ──────────────────────────────────────────────
# These IP ranges must NEVER be crawled. An attacker could submit
# http://169.254.169.254/latest/meta-data/ to steal cloud credentials
# (AWS/GCP metadata endpoint), or http://127.0.0.1:6379 to probe
# internal services. Blocking private/reserved ranges prevents this.

BLOCKED_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918 – private
    ipaddress.ip_network("172.16.0.0/12"),      # RFC1918 – private
    ipaddress.ip_network("192.168.0.0/16"),     # RFC1918 – private
    ipaddress.ip_network("127.0.0.0/8"),        # loopback
    ipaddress.ip_network("169.254.0.0/16"),     # link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),          # "this" network
    ipaddress.ip_network("100.64.0.0/10"),      # carrier-grade NAT
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),      # benchmarking
    ipaddress.ip_network("::1/128"),             # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),            # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),           # IPv6 link-local
]


def validate_url_safety(url: str) -> None:
    """
    Block SSRF (Server-Side Request Forgery) attacks.

    Resolves the URL's hostname to an IP address and checks it against
    known private/internal/cloud-metadata IP ranges. Raises ValueError
    if the URL is unsafe.

    Why this matters:
        CloudSEK's product crawls arbitrary user-supplied URLs. Without
        this check, an attacker could:
        - Read AWS/GCP instance metadata (169.254.169.254)
        - Port-scan internal services (127.0.0.1, 10.x.x.x)
        - Access databases on the private network (192.168.x.x)
    """
    parsed = urlparse(url)
    hostname = parsed.hostname

    if not hostname:
        raise ValueError("Cannot extract hostname from URL")

    # Block obviously dangerous schemes
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

    try:
        resolved_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}")

    ip = ipaddress.ip_address(resolved_ip)

    for blocked in BLOCKED_IP_RANGES:
        if ip in blocked:
            raise ValueError(
                f"URL resolves to blocked IP range ({resolved_ip}). "
                f"Internal/cloud metadata URLs are not allowed."
            )

    logger.info("SSRF check passed: %s → %s", hostname, resolved_ip)


# ──────────────────────────────────────────────
# CORE: FETCH METADATA
# ──────────────────────────────────────────────

def fetch_metadata(url: str) -> MetadataDocument:
    """
    Make an HTTP GET request to *url* and return a `MetadataDocument`
    populated with headers, cookies, page source, and status code.

    Raises
    ------
    requests.exceptions.RequestException
        On any network / HTTP error (timeout, DNS failure, etc.).
    """
    # ── SSRF guard: reject private/internal IPs before making the request ──
    validate_url_safety(url)

    response = http_requests.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    # Convert cookie jar → list of CookieItem dicts
    cookies = [
        CookieItem(
            name=cookie.name,
            value=cookie.value,
            domain=cookie.domain,
            path=cookie.path,
        )
        for cookie in response.cookies
    ]

    return MetadataDocument(
        url=url,
        headers=dict(response.headers),
        cookies=cookies,
        page_source=response.text,
        status_code=response.status_code,
        collected_at=datetime.now(UTC),
    )


# ──────────────────────────────────────────────
# DB: STORE & RETRIEVE
# ──────────────────────────────────────────────

def store_metadata(collection: Collection, doc: MetadataDocument) -> str:
    """
    Upsert metadata into MongoDB.

    Uses `url` as the match key so that re-collecting the same URL
    overwrites the old record rather than creating a duplicate.

    Returns the string representation of the upserted document's `_id`.
    """
    payload = doc.model_dump(exclude={"id"})
    # Convert CookieItem objects to dicts for Mongo storage
    payload["cookies"] = [c.model_dump() for c in doc.cookies]

    result = collection.update_one(
        {"url": doc.url},       # filter
        {"$set": payload},      # update
        upsert=True,            # insert if not found
    )

    # Return the _id (either the matched doc or the newly inserted one)
    if result.upserted_id:
        return str(result.upserted_id)

    existing = collection.find_one({"url": doc.url})
    return str(existing["_id"]) if existing else ""


def find_metadata(collection: Collection, url: str) -> Optional[Dict[str, Any]]:
    """
    Look up a stored metadata document by URL.

    Returns the raw Mongo document (dict) or None if not found.
    """
    doc = collection.find_one({"url": url})
    if doc:
        # Convert ObjectId → str so JSON serialisation works
        doc["_id"] = str(doc["_id"])
    return doc


# ──────────────────────────────────────────────
# BACKGROUND COLLECTION (used by GET-miss flow)
# ──────────────────────────────────────────────

def collect_metadata_in_background(collection: Collection, url: str) -> None:
    """
    Spawn a daemon thread that fetches metadata for *url* and stores it.

    This is the "internally trigger metadata collection" requirement:
    the GET endpoint returns immediately with a message, and the actual
    HTTP call + DB write happens asynchronously.
    """

    def _worker() -> None:
        try:
            logger.info("Background collection started for %s", url)
            doc = fetch_metadata(url)
            store_metadata(collection, doc)
            logger.info("Background collection completed for %s", url)
        except Exception:
            logger.exception("Background collection failed for %s", url)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


# ──────────────────────────────────────────────
# ORCHESTRATION HELPERS
# ──────────────────────────────────────────────

def collect_and_store(collection: Collection, url: str) -> MetadataDocument:
    """
    Synchronous end-to-end: fetch + store.  Used by the POST endpoint.

    Returns the populated MetadataDocument so the caller can build a response.
    """
    doc = fetch_metadata(url)
    store_metadata(collection, doc)
    return doc
