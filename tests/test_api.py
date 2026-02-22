"""
test_api.py

Tests for the metadata API.
Uses mongomock so we don't need a real MongoDB running.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import mongomock
import pytest
from fastapi.testclient import TestClient

# We need to swap out the real Mongo client with a fake one
# BEFORE importing our app, otherwise it tries to connect for real

_mock_client = mongomock.MongoClient()


def _patched_get_client():
    return _mock_client


patch("app.database.get_client", _patched_get_client).start()

from app.main import app  # noqa: E402 — has to come after the patch

client = TestClient(app)

# --- Setup ---

TEST_URL = "https://httpbin.org/html"


@pytest.fixture(autouse=True)
def _clean_db():
    """Wipe the DB before and after each test so they don't interfere."""
    _mock_client.drop_database("metadata_inventory")
    yield
    _mock_client.drop_database("metadata_inventory")


# Fake HTTP response so we don't actually hit the internet during tests
def _fake_requests_get(url, **kwargs):
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8", "Server": "mock"}
    mock_resp.text = "<html><body>Hello, World!</body></html>"
    mock_resp.status_code = 200

    mock_resp.cookies = []  # no cookies
    return mock_resp


# --- Tests ---

@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_post_metadata_success(mock_get):
    """POST a URL, should get back the metadata."""
    response = client.post("/metadata", json={"url": TEST_URL})

    assert response.status_code == 200
    data = response.json()
    assert data["url"] == TEST_URL
    assert "Content-Type" in data["headers"]
    assert data["page_source"] == "<html><body>Hello, World!</body></html>"
    assert data["status_code"] == 200
    assert "collected_at" in data


@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_get_metadata_existing(mock_get):
    """Save data via POST first, then GET should find it."""
    # Seed
    client.post("/metadata", json={"url": TEST_URL})

    # Retrieve
    response = client.get("/metadata", params={"url": TEST_URL})

    assert response.status_code == 200
    data = response.json()
    assert data["url"] == TEST_URL
    assert data["page_source"] == "<html><body>Hello, World!</body></html>"


@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_get_metadata_missing_triggers_background(mock_get):
    """GET for unknown URL should say 'check later' and fetch in background."""
    response = client.get("/metadata", params={"url": TEST_URL})

    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "please check later" in data["message"].lower()

    # wait a sec for the background thread
    time.sleep(1)

    # now it should be there
    response2 = client.get("/metadata", params={"url": TEST_URL})
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["url"] == TEST_URL
    assert data2["page_source"] == "<html><body>Hello, World!</body></html>"


def test_post_metadata_invalid_url():
    """Garbage URL should fail validation."""
    response = client.post("/metadata", json={"url": "not-a-valid-url"})
    assert response.status_code == 422


def test_get_metadata_bad_scheme():
    """URL without http:// should get rejected."""
    response = client.get("/metadata", params={"url": "example.com"})
    assert response.status_code == 400
    assert "http" in response.json()["detail"].lower()


def test_health_check():
    """Health endpoint should always be 200."""
    response = client.get("/health")
    assert response.status_code == 200
    assert "ok" in response.json()["message"]


@patch("app.services.http_requests.get", side_effect=_fake_requests_get)
def test_post_metadata_duplicate_upserts(mock_get):
    """POSTing the same URL twice shouldn't create duplicates."""
    resp1 = client.post("/metadata", json={"url": TEST_URL})
    resp2 = client.post("/metadata", json={"url": TEST_URL})

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # should only be one document, not two
    db = _mock_client["metadata_inventory"]
    count = db["metadata"].count_documents({"url": TEST_URL})
    assert count == 1


# --- SSRF protection tests ---

@patch("app.services.socket.gethostbyname", return_value="127.0.0.1")
def test_post_metadata_ssrf_localhost(mock_dns):
    """Localhost should be blocked — classic SSRF target."""
    response = client.post("/metadata", json={"url": "http://localhost:6379"})
    assert response.status_code == 403
    assert "blocked" in response.json()["detail"].lower()


@patch("app.services.socket.gethostbyname", return_value="169.254.169.254")
def test_post_metadata_ssrf_aws_metadata(mock_dns):
    """AWS metadata IP should be blocked — this is the classic cloud SSRF."""
    response = client.post("/metadata", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert response.status_code == 403
    assert "blocked" in response.json()["detail"].lower()


@patch("app.services.socket.gethostbyname", return_value="10.0.0.1")
def test_get_metadata_ssrf_private_ip(mock_dns):
    """Private IPs should be blocked on GET too, not just POST."""
    response = client.get("/metadata", params={"url": "http://internal-service.corp/admin"})
    assert response.status_code == 403
    assert "blocked" in response.json()["detail"].lower()
