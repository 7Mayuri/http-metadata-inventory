# HTTP Metadata Inventory Service

A production-ready FastAPI microservice that collects and stores HTTP metadata (headers, cookies, page source) for any given URL, backed by MongoDB.

---

## Architecture

```
┌────────────┐       ┌────────────────┐       ┌──────────┐
│   Client    │──────▶│  FastAPI (api)  │──────▶│  MongoDB  │
│  (curl/UI)  │◀──────│   :8000        │◀──────│  (mongo)  │
└────────────┘       └────────────────┘       └──────────┘
```

| Layer | File | Purpose |
|-------|------|---------|
| Routes | `app/main.py` | HTTP endpoints, lifespan, dependency injection |
| Models | `app/models.py` | Pydantic request / response schemas |
| Services | `app/services.py` | Business logic – fetch, store, background jobs |
| Database | `app/database.py` | MongoDB connection manager, indexes |
| Tests | `tests/test_api.py` | Pytest suite with mongomock |

---

## Prerequisites

- **Docker** ≥ 20.10
- **Docker Compose** ≥ 2.0

---

## Quick Start

```bash
# Clone the repo and cd into it
cd metadata-inventory

# Start everything (builds the image on first run)
docker-compose up --build
```

The API will be available at **http://localhost:8000**.

Interactive docs: **http://localhost:8000/docs**

---

## API Reference

### `POST /metadata`

Collect metadata for a URL and store it in MongoDB.

**Request body:**
```json
{
  "url": "https://example.com"
}
```

**Response (200):**
```json
{
  "url": "https://example.com",
  "headers": { "Content-Type": "text/html", ... },
  "cookies": [],
  "page_source": "<html>...</html>",
  "status_code": 200,
  "collected_at": "2026-02-20T12:00:00"
}
```

### `GET /metadata?url=https://example.com`

Retrieve stored metadata. If the URL isn't in the database, background collection is triggered.

**Response when found (200):**
```json
{
  "url": "https://example.com",
  "headers": { ... },
  "cookies": [],
  "page_source": "<html>...</html>",
  "status_code": 200,
  "collected_at": "2026-02-20T12:00:00"
}
```

**Response when NOT found (200):**
```json
{
  "message": "Record doesn't exist & request has been logged to collect the metadata, please check later"
}
```

### `GET /health`

```json
{ "message": "ok – mongo=connected" }
```

---

## Running Tests (locally)

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Tests use **mongomock** so no running MongoDB instance is needed.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGO_HOST` | `mongo` | MongoDB hostname (Docker service name) |
| `MONGO_PORT` | `27017` | MongoDB port |
| `MONGO_DB` | `metadata_inventory` | Database name |
| `REQUEST_TIMEOUT` | `10` | HTTP request timeout in seconds |

---

## Project Structure

```
metadata-inventory/
├── app/
│   ├── __init__.py       # Package marker
│   ├── main.py           # FastAPI app, routes, lifespan hooks
│   ├── database.py       # MongoDB connection & index management
│   ├── models.py         # Pydantic schemas
│   └── services.py       # Business logic (fetch, store, background)
├── tests/
│   ├── __init__.py
│   └── test_api.py       # Pytest suite (7 tests)
├── .dockerignore
├── .env                  # Default env vars
├── Dockerfile            # Python 3.12-slim image
├── docker-compose.yml    # Orchestrates api + mongo
├── requirements.txt      # Pinned dependencies
└── README.md             # This file
```

---

## Design Decisions

1. **pymongo (sync)** over motor (async) – as required by the spec; keeps the code straightforward.
2. **Upsert on `url`** – prevents duplicate documents; re-POSTing refreshes the metadata.
3. **Background thread for GET-miss** – the GET endpoint returns immediately; a daemon thread handles the HTTP fetch + DB write.
4. **mongomock for tests** – tests run in-memory with zero external dependencies.
5. **Dependency injection** – `get_db` / `get_collection` are injected via FastAPI's `Depends()`, making them easy to override in tests.

---

## License

MIT
