"""
models.py – Pydantic schemas for request validation and response serialisation.

Keeps a strict boundary between what the API accepts / returns and what
lives in MongoDB.  Every field is documented so Swagger/OpenAPI docs are
self-explanatory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


# ──────────────────────────────────────────────
# REQUEST MODELS
# ──────────────────────────────────────────────

class URLRequest(BaseModel):
    """Body for POST /metadata – the only required field is a valid HTTP(S) URL."""
    url: HttpUrl = Field(
        ...,
        description="Fully-qualified URL to collect metadata from (must start with http:// or https://)",
        examples=["https://example.com"],
    )


# ──────────────────────────────────────────────
# INTERNAL / DB MODELS
# ──────────────────────────────────────────────

class CookieItem(BaseModel):
    """Simplified representation of an HTTP cookie."""
    model_config = ConfigDict(extra="allow")

    name: str
    value: str
    domain: Optional[str] = None
    path: Optional[str] = None


class MetadataDocument(BaseModel):
    """
    The document shape stored in MongoDB.
    `id` is mapped from Mongo's `_id` for JSON responses.
    """
    id: Optional[str] = Field(None, alias="_id", description="MongoDB ObjectId as string")
    url: str = Field(..., description="Canonical URL that was crawled")
    headers: Dict[str, str] = Field(default_factory=dict, description="HTTP response headers")
    cookies: List[CookieItem] = Field(default_factory=list, description="Cookies set by the server")
    page_source: str = Field("", description="Raw HTML page source")
    status_code: int = Field(0, description="HTTP status code of the response")
    collected_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp of collection",
    )

    model_config = ConfigDict(populate_by_name=True)


# ──────────────────────────────────────────────
# RESPONSE MODELS
# ──────────────────────────────────────────────

class MetadataResponse(BaseModel):
    """Successful response containing collected metadata."""
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "url": "https://example.com",
                "headers": {"Content-Type": "text/html"},
                "cookies": [{"name": "sid", "value": "abc123"}],
                "page_source": "<html>...</html>",
                "status_code": 200,
                "collected_at": "2026-02-20T12:00:00",
            }
        }
    )

    url: str
    headers: Dict[str, str]
    cookies: List[CookieItem]
    page_source: str
    status_code: int
    collected_at: str  # ISO-8601 string for JSON friendliness


class MessageResponse(BaseModel):
    """Generic message response (used for GET-miss and error cases)."""
    message: str


class ErrorResponse(BaseModel):
    """Structured error payload."""
    detail: str
