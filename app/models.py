"""
models.py

All the request/response shapes live here.
Pydantic handles the validation so I don't have to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


# --- Request ---

class URLRequest(BaseModel):
    """What the user sends us — just a URL."""
    url: HttpUrl = Field(
        ...,
        description="URL to collect metadata from",
        examples=["https://example.com"],
    )


# --- What we store in MongoDB ---

class CookieItem(BaseModel):
    """One cookie from the response. Keeping it simple."""
    model_config = ConfigDict(extra="allow")

    name: str
    value: str
    domain: Optional[str] = None
    path: Optional[str] = None


class MetadataDocument(BaseModel):
    """This is what one document looks like in our MongoDB collection."""
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


# --- API responses ---

class MetadataResponse(BaseModel):
    """What we send back when metadata is found."""
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
    """Simple text message — used for "check back later" responses."""
    message: str


class ErrorResponse(BaseModel):
    """When something goes wrong."""
    detail: str
