"""
test_api.py – Pytest suite for the Metadata Inventory API.

Uses `mongomock` to patch pymongo so tests run without a real MongoDB
instance.  The FastAPI TestClient (backed by httpx) is used for HTTP calls.

Tests cover:
  1. POST /metadata  – successful collection
  2. GET  /metadata  – existing record
  3. GET  /metadata  – missing record (triggers background collection)
  4. POST /metadata  – invalid URL
  5. GET  /metadata  – URL without scheme
  6. Health endpoint
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import mongomock
import pytest
from fastapi.testclient import TestClient

# ──────────────────────────────────────────────
# Patch pymongo BEFORE importing the app so that
# database.py creates a mongomock client instead.
# ──────────────────────────────────────────────

_mock_client = mongomock.MongoClient()


def _patched_get_client():
    return _mock_client


# Patch at the module level so every import sees the mock
patch("app.database.get_client", _patched_get_client).start()

from app.main import app  # noqa: E402 – must come after patch

client = TestClient(app)

# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

TEST_URL = "https://httpbin.org/html"


@pytest.fixture(autouse=True)
def _clean_db():
    """Drop the test collection before each test for isolation."""
    _mock_client.drop_database("metadata_inventory")
    yield
    _mock_client.drop_database("metadata_inventory")


# ──────────────────────────────────────────────
# Helpers – mock HTTP responses
# ──────────────────────────────────────────────

def _fake_requests_get(url, **kwargs):
    """Return a deterministic mock response for `requests.get`."""
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8", "Server": "mock"}
    mock_resp.text = "<html><body>Hello, World!</body></html>"
    mock_resp.status_code = 200

    # Simulate an empty cookie jar
    mock_resp.cookies = []
    return mock_resp


# ──────────────────────────────────────────────
# 1) POST /metadata – happy path
# ──────────────────────────────────────────────

@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_post_metadata_success(mock_get):
    """POST should fetch metadata, store it, and return the result."""
    response = client.post("/metadata", json={"url": TEST_URL})

    assert response.status_code == 200
    data = response.json()
    assert data["url"] == TEST_URL
    assert "Content-Type" in data["headers"]
    assert data["page_source"] == "<html><body>Hello, World!</body></html>"
    assert data["status_code"] == 200
    assert "collected_at" in data


# ──────────────────────────────────────────────
# 2) GET /metadata – record EXISTS
# ──────────────────────────────────────────────

@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_get_metadata_existing(mock_get):
    """Seed data via POST, then GET should return the stored record."""
    # Seed
    client.post("/metadata", json={"url": TEST_URL})

    # Retrieve
    response = client.get("/metadata", params={"url": TEST_URL})

    assert response.status_code == 200
    data = response.json()
    assert data["url"] == TEST_URL
    assert data["page_source"] == "<html><body>Hello, World!</body></html>"


# ──────────────────────────────────────────────
# 3) GET /metadata – record MISSING
# ──────────────────────────────────────────────

@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_get_metadata_missing_triggers_background(mock_get):
    """
    GET for an unknown URL should return 200 with a 'please check later'
    message AND trigger background collection.
    """
    response = client.get("/metadata", params={"url": TEST_URL})

    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "please check later" in data["message"].lower()

    # Wait a moment for the background thread to complete
    time.sleep(1)

    # Now the data should be available
    response2 = client.get("/metadata", params={"url": TEST_URL})
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["url"] == TEST_URL
    assert data2["page_source"] == "<html><body>Hello, World!</body></html>"


# ──────────────────────────────────────────────
# 4) POST /metadata – invalid URL
# ──────────────────────────────────────────────

def test_post_metadata_invalid_url():
    """Supplying a non-URL string should fail validation (422)."""
    response = client.post("/metadata", json={"url": "not-a-valid-url"})
    assert response.status_code == 422


# ──────────────────────────────────────────────
# 5) GET /metadata – URL without scheme
# ──────────────────────────────────────────────

def test_get_metadata_bad_scheme():
    """GET with a URL lacking http(s):// should return 400."""
    response = client.get("/metadata", params={"url": "example.com"})
    assert response.status_code == 400
    assert "http" in response.json()["detail"].lower()


# ──────────────────────────────────────────────
# 6) Health check
# ──────────────────────────────────────────────

def test_health_check():
    """The /health endpoint should always return 200."""
    response = client.get("/health")
    assert response.status_code == 200
    assert "ok" in response.json()["message"]


# ──────────────────────────────────────────────
# 7) POST /metadata – duplicate URL (upsert)
# ──────────────────────────────────────────────

@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_post_metadata_duplicate_upserts(mock_get):
    """
    Posting the same URL twice should NOT create a duplicate –
    the second call should update (upsert) the existing record.
    """
    resp1 = client.post("/metadata", json={"url": TEST_URL})
    resp2 = client.post("/metadata", json={"url": TEST_URL})

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # Verify only one document exists in the collection
    db = _mock_client["metadata_inventory"]
    count = db["metadata"].count_documents({"url": TEST_URL})
    assert count == 1


# ──────────────────────────────────────────────
# 8) POST /metadata – SSRF: localhost
# ──────────────────────────────────────────────

@patch("app.services.socket.gethostbyname", return_value="127.0.0.1")
def test_post_metadata_ssrf_localhost(mock_dns):
    """
    POST with a URL that resolves to 127.0.0.1 (loopback) should be
    blocked with 403 — prevents SSRF attacks against internal services.
    """
    response = client.post("/metadata", json={"url": "http://localhost:6379"})
    assert response.status_code == 403
    assert "blocked" in response.json()["detail"].lower()


# ──────────────────────────────────────────────
# 9) POST /metadata – SSRF: AWS metadata endpoint
# ──────────────────────────────────────────────

@patch("app.services.socket.gethostbyname", return_value="169.254.169.254")
def test_post_metadata_ssrf_aws_metadata(mock_dns):
    """
    POST with the AWS metadata IP (169.254.169.254) should be blocked.
    This is the classic cloud SSRF attack vector.
    """
    response = client.post("/metadata", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert response.status_code == 403
    assert "blocked" in response.json()["detail"].lower()


# ──────────────────────────────────────────────
# 10) GET /metadata – SSRF: private network
# ──────────────────────────────────────────────

@patch("app.services.socket.gethostbyname", return_value="10.0.0.1")
def test_get_metadata_ssrf_private_ip(mock_dns):
    """
    GET with a URL resolving to a private IP range (10.x.x.x) should
    be blocked even on the GET endpoint — SSRF check runs before
    background collection is triggered.
    """
    response = client.get("/metadata", params={"url": "http://internal-service.corp/admin"})
    assert response.status_code == 403
    assert "blocked" in response.json()["detail"].lower()
