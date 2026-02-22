"""
main.py

This is the entry point. All the routes are defined here.
POST /metadata  — crawl a URL and save the result
GET  /metadata  — look up saved data (or start background fetch)
GET  /health    — simple health check for Docker
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

# Basic logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# Runs on startup and shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Check Mongo on startup, close connection on shutdown."""
    logger.info("Starting up — checking if MongoDB is alive...")
    if ping():
        logger.info("MongoDB is good")
        get_collection()  # create indexes early
    else:
        logger.warning("MongoDB not reachable right now, will retry on first request")

    yield  # app runs here

    logger.info("Shutting down...")
    close_connection()


# The actual app
app = FastAPI(
    title="HTTP Metadata Inventory Service",
    description=(
        "Collects and stores HTTP metadata (headers, cookies, page source) "
        "for any given URL.  Built with FastAPI + MongoDB."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# Dependency injection — gives route handlers the mongo collection directly
def _collection(db: Database = Depends(get_db)):
    return get_collection(db)


# --- Health check ---

@app.get(
    "/health",
    tags=["ops"],
    summary="Service health-check",
    response_model=MessageResponse,
)
def health_check():
    """Quick check — is the API up and can it talk to Mongo?"""
    mongo_ok = ping()
    return MessageResponse(
        message=f"ok – mongo={'connected' if mongo_ok else 'unreachable'}"
    )


# --- POST /metadata ---

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
    """Crawl the URL right now, save it, return the data."""
    url = str(body.url)
    logger.info("POST /metadata — url=%s", url)

    try:
        doc = collect_and_store(collection, url)
    except ValueError as exc:
        # SSRF — someone tried to hit an internal IP
        logger.warning("Blocked URL %s: %s", url, exc)
        raise HTTPException(
            status_code=403,
            detail=f"URL blocked: {exc}",
        )
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


# --- GET /metadata ---

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
    """Return saved data if we have it, otherwise kick off a background fetch."""
    logger.info("GET /metadata — url=%s", url)

    # basic check — needs to be a proper URL
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="URL must start with http:// or https://",
        )

    # run SSRF check on GET too, not just POST
    from app.services import validate_url_safety
    try:
        validate_url_safety(url)
    except ValueError as exc:
        logger.warning("Blocked URL on GET %s: %s", url, exc)
        raise HTTPException(
            status_code=403,
            detail=f"URL blocked: {exc}",
        )

    # check if we already have it
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

    # don't have it yet — fetch in background so user doesn't wait
    collect_metadata_in_background(collection, url)

    return MessageResponse(
        message=(
            "Record doesn't exist & request has been logged "
            "to collect the metadata, please check later"
        ),
    )
