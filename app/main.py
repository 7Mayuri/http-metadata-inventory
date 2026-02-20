"""
main.py – FastAPI application entry-point.

Responsibilities
────────────────
• Define the FastAPI app with lifespan (startup / shutdown hooks).
• Wire up two endpoints:
      POST /metadata   – collect & store metadata for a URL
      GET  /metadata   – retrieve stored metadata (or trigger background collection)
• Provide a lightweight /health endpoint for Docker health-checks.
• Use dependency injection (`Depends`) to pass the DB handle to routes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Union

from fastapi import Depends, FastAPI, HTTPException, Query
from pymongo.database import Database
from requests.exceptions import RequestException

from app.database import close_connection, get_collection, get_db, ping
from app.models import (
    ErrorResponse,
    MessageResponse,
    MetadataResponse,
    URLRequest,
)
from app.services import (
    collect_and_store,
    collect_metadata_in_background,
    find_metadata,
)

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# LIFESPAN (replaces deprecated on_event)
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: verify Mongo is reachable + create indexes.
    Shutdown: close the Mongo client gracefully.
    """
    logger.info("Starting up – checking MongoDB connectivity …")
    if ping():
        logger.info("MongoDB is reachable ✓")
        # Ensure indexes exist before we serve traffic
        get_collection()
    else:
        logger.warning("MongoDB is NOT reachable – the app will retry on first request")

    yield  # ← application runs here

    logger.info("Shutting down – closing MongoDB connection …")
    close_connection()


# ──────────────────────────────────────────────
# APP INSTANCE
# ──────────────────────────────────────────────

app = FastAPI(
    title="HTTP Metadata Inventory Service",
    description=(
        "Collects and stores HTTP metadata (headers, cookies, page source) "
        "for any given URL.  Built with FastAPI + MongoDB."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# DEPENDENCY SHORTCUT
# ──────────────────────────────────────────────

def _collection(db: Database = Depends(get_db)):
    """Inject the `metadata` collection directly into handlers."""
    return get_collection(db)


# ──────────────────────────────────────────────
# HEALTH-CHECK
# ──────────────────────────────────────────────

@app.get(
    "/health",
    tags=["ops"],
    summary="Service health-check",
    response_model=MessageResponse,
)
def health_check():
    """Returns 200 if the API is up; includes Mongo connectivity status."""
    mongo_ok = ping()
    return MessageResponse(
        message=f"ok – mongo={'connected' if mongo_ok else 'unreachable'}"
    )


# ──────────────────────────────────────────────
# POST /metadata
# ──────────────────────────────────────────────

@app.post(
    "/metadata",
    tags=["metadata"],
    summary="Collect metadata for a URL",
    response_model=MetadataResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or unreachable URL"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
def post_metadata(
    body: URLRequest,
    collection=Depends(_collection),
):
    """
    Accept a URL, crawl it synchronously, store the metadata in MongoDB,
    and return the collected data.
    """
    url = str(body.url)
    logger.info("POST /metadata – url=%s", url)

    try:
        doc = collect_and_store(collection, url)
    except RequestException as exc:
        logger.error("Failed to fetch %s: %s", url, exc)
        raise HTTPException(
            status_code=400,
            detail=f"Could not fetch URL: {exc}",
        )
    except Exception as exc:
        logger.exception("Unexpected error while collecting metadata for %s", url)
        raise HTTPException(
            status_code=500,
            detail=f"Internal error: {exc}",
        )

    return MetadataResponse(
        url=doc.url,
        headers=doc.headers,
        cookies=doc.cookies,
        page_source=doc.page_source,
        status_code=doc.status_code,
        collected_at=doc.collected_at.isoformat(),
    )


# ──────────────────────────────────────────────
# GET /metadata
# ──────────────────────────────────────────────

@app.get(
    "/metadata",
    tags=["metadata"],
    summary="Retrieve stored metadata for a URL",
    response_model=Union[MetadataResponse, MessageResponse],
    responses={
        200: {
            "description": "Metadata found or collection scheduled",
            "content": {
                "application/json": {
                    "examples": {
                        "found": {
                            "summary": "Metadata exists",
                            "value": {
                                "url": "https://example.com",
                                "headers": {},
                                "cookies": [],
                                "page_source": "<html></html>",
                                "status_code": 200,
                                "collected_at": "2026-02-20T12:00:00",
                            },
                        },
                        "missing": {
                            "summary": "Metadata not yet collected",
                            "value": {
                                "message": (
                                    "Record doesn't exist & request has been logged "
                                    "to collect the metadata, please check later"
                                )
                            },
                        },
                    },
                },
            },
        },
        400: {"model": ErrorResponse, "description": "Invalid URL supplied"},
    },
)
def get_metadata(
    url: str = Query(
        ...,
        description="The URL whose metadata you want to retrieve",
        examples=["https://example.com"],
    ),
    collection=Depends(_collection),
):
    """
    1. If the URL already exists in MongoDB → return the stored metadata.
    2. If it does NOT exist → trigger background collection (no POST call)
       and return a "please check later" message.
    """
    logger.info("GET /metadata – url=%s", url)

    # ── Quick URL sanity check ──
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="URL must start with http:// or https://",
        )

    # ── Lookup in Mongo ──
    existing = find_metadata(collection, url)

    if existing:
        return MetadataResponse(
            url=existing["url"],
            headers=existing.get("headers", {}),
            cookies=existing.get("cookies", []),
            page_source=existing.get("page_source", ""),
            status_code=existing.get("status_code", 0),
            collected_at=(
                existing["collected_at"].isoformat()
                if hasattr(existing.get("collected_at"), "isoformat")
                else str(existing.get("collected_at", ""))
            ),
        )

    # ── Not found → schedule background collection ──
    collect_metadata_in_background(collection, url)

    return MessageResponse(
        message=(
            "Record doesn't exist & request has been logged "
            "to collect the metadata, please check later"
        ),
    )
