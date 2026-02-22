"""
services.py

This is where the actual work happens.

- Crawl a URL and pull out its headers, cookies, and HTML
- Save that data to MongoDB (no duplicates)
- Look up saved data by URL
- If a URL isn't in the DB yet, kick off a background fetch so it's ready next time
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

import requests as http_requests  # renamed so it doesn't clash with FastAPI's Request
from pymongo.collection import Collection

from app.models import CookieItem, MetadataDocument

logger = logging.getLogger(__name__)

# How long to wait before giving up on a slow site
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "10"))

# Pretend to be a real browser so sites don't block us immediately
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

# These are IP ranges that only exist inside private networks or cloud infrastructure.
# We never want our server making requests to these — an attacker could submit
# something like http://169.254.169.254 (AWS metadata endpoint) and our server
# would happily fetch it, leaking cloud credentials.
BLOCKED_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),        # private network
    ipaddress.ip_network("172.16.0.0/12"),      # private network
    ipaddress.ip_network("192.168.0.0/16"),     # private network (home/office routers)
    ipaddress.ip_network("127.0.0.0/8"),        # localhost
    ipaddress.ip_network("169.254.0.0/16"),     # AWS/GCP metadata endpoint lives here
    ipaddress.ip_network("0.0.0.0/8"),          # invalid source address
    ipaddress.ip_network("100.64.0.0/10"),      # carrier NAT, not public internet
    ipaddress.ip_network("192.0.0.0/24"),       # reserved by IETF
    ipaddress.ip_network("198.18.0.0/15"),      # used for benchmarking, not real sites
    ipaddress.ip_network("::1/128"),             # IPv6 localhost
    ipaddress.ip_network("fc00::/7"),            # IPv6 private network
    ipaddress.ip_network("fe80::/10"),           # IPv6 link-local
]


def validate_url_safety(url: str) -> None:
    """
    Before we crawl anything, make sure the URL isn't pointing at
    something inside our own infrastructure.

    We resolve the hostname to an IP first, then check if that IP
    is in any of the blocked ranges. If it is, we raise an error
    and never make the HTTP request.

    This stops SSRF attacks — where someone tricks our server into
    fetching internal services like databases, admin panels, or
    cloud metadata endpoints.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname

    if not hostname:
        raise ValueError("Cannot extract hostname from URL")

    # Only allow regular web URLs
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

    # Resolve hostname → IP. If it can't be resolved, reject it.
    try:
        resolved_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}")

    ip = ipaddress.ip_address(resolved_ip)

    # Check the resolved IP against every blocked range
    for blocked in BLOCKED_IP_RANGES:
        if ip in blocked:
            raise ValueError(
                f"URL resolves to blocked IP range ({resolved_ip}). "
                f"Internal/cloud metadata URLs are not allowed."
            )

    logger.info("SSRF check passed: %s → %s", hostname, resolved_ip)


# ──────────────────────────────────────────────
# FETCH
# ──────────────────────────────────────────────

def fetch_metadata(url: str) -> MetadataDocument:
    """
    Crawl the given URL and return everything we collected —
    headers, cookies, page source, and status code.

    Raises requests.exceptions.RequestException if the site is
    unreachable, times out, or returns a network error.
    """
    # Safety first — block internal/private URLs before making any request
    validate_url_safety(url)

    response = http_requests.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,  # follow redirects like a browser would
    )

    # Turn the cookie jar into a plain list of objects we can store
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
# STORE & RETRIEVE
# ──────────────────────────────────────────────

def store_metadata(collection: Collection, doc: MetadataDocument) -> str:
    """
    Save metadata to MongoDB.

    We use an upsert — if a document with this URL already exists,
    we update it. If not, we insert a new one. This way the same URL
    can never create duplicate records.

    Returns the MongoDB _id of the saved document.
    """
    payload = doc.model_dump(exclude={"id"})
    # CookieItem objects need to be dicts before going into Mongo
    payload["cookies"] = [c.model_dump() for c in doc.cookies]

    result = collection.update_one(
        {"url": doc.url},   # find document by URL
        {"$set": payload},  # overwrite all fields
        upsert=True,        # create it if it doesn't exist yet
    )

    # Return the _id — either the new one or the existing one
    if result.upserted_id:
        return str(result.upserted_id)

    existing = collection.find_one({"url": doc.url})
    return str(existing["_id"]) if existing else ""


def find_metadata(collection: Collection, url: str) -> Optional[Dict[str, Any]]:
    """
    Look up a URL in the database.

    Returns the document as a dict, or None if we haven't crawled it yet.
    """
    doc = collection.find_one({"url": url})
    if doc:
        doc["_id"] = str(doc["_id"])  # ObjectId isn't JSON serialisable, convert it
    return doc


# ──────────────────────────────────────────────
# BACKGROUND COLLECTION
# ──────────────────────────────────────────────

def collect_metadata_in_background(collection: Collection, url: str) -> None:
    """
    When a GET request comes in for a URL we haven't seen before,
    we don't want to make the user wait. So we return a response immediately
    and do the actual crawling in a background thread.

    By the time they check again, the data should be there.
    daemon=True means this thread won't block the server from shutting down.
    """

    def _worker() -> None:
        try:
            logger.info("Background collection started for %s", url)
            doc = fetch_metadata(url)
            store_metadata(collection, doc)
            logger.info("Background collection done for %s", url)
        except Exception:
            logger.exception("Background collection failed for %s", url)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


# ──────────────────────────────────────────────
# ORCHESTRATION
# ──────────────────────────────────────────────

def collect_and_store(collection: Collection, url: str) -> MetadataDocument:
    """
    Fetch a URL and save the result in one go.
    This is what the POST endpoint calls — it needs the data back immediately.
    """
    doc = fetch_metadata(url)
    store_metadata(collection, doc)
    return doc
