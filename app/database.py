"""
database.py – MongoDB connection manager.

Responsibilities
────────────────
• Read connection params from environment variables (with sensible defaults).
• Provide a *singleton* MongoClient so the app doesn't open a new connection
  per request.
• Expose a `get_db()` dependency that FastAPI injects into route handlers.
• Create a unique index on `url` to prevent duplicate documents.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database


# ──────────────────────────────────────────────
# CONFIGURATION (read once from env)
# ──────────────────────────────────────────────

MONGO_HOST: str = os.getenv("MONGO_HOST", "localhost")
MONGO_PORT: int = int(os.getenv("MONGO_PORT", "27017"))
MONGO_DB: str = os.getenv("MONGO_DB", "metadata_inventory")

# Full URI (supports authentication if added later)
MONGO_URI: str = os.getenv(
    "MONGO_URI",
    f"mongodb://{MONGO_HOST}:{MONGO_PORT}",
)


# ──────────────────────────────────────────────
# SINGLETON CLIENT
# ──────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    """
    Return a cached MongoClient.  `lru_cache` ensures we only ever create
    one client across the entire application lifetime.
    """
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5000,  # fail fast if Mongo is unreachable
    )
    return client


def get_db() -> Database:
    """FastAPI dependency – returns the application database handle."""
    return get_client()[MONGO_DB]


def get_collection(db: Database | None = None) -> Collection:
    """
    Convenience accessor for the `metadata` collection.

    Also ensures the unique index on `url` exists (idempotent operation).
    """
    if db is None:
        db = get_db()
    collection = db["metadata"]
    # Unique index prevents duplicate URL entries; `background=True` avoids
    # blocking writes while the index is being built.
    collection.create_index("url", unique=True, background=True)
    return collection


# ──────────────────────────────────────────────
# LIFECYCLE HELPERS (called from main.py)
# ──────────────────────────────────────────────

def ping() -> bool:
    """Health-check: returns True if Mongo responds to a ping."""
    try:
        get_client().admin.command("ping")
        return True
    except Exception:
        return False


def close_connection() -> None:
    """Gracefully close the MongoClient on shutdown."""
    get_client().close()
    get_client.cache_clear()  # reset lru_cache so a new client can be created
