"""
database.py

Handles the MongoDB connection.
Reads config from env vars, keeps one shared client for the whole app,
and sets up the url index so we don't get duplicates.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database


# Connection config — defaults work for local dev, override with env vars in Docker
MONGO_HOST: str = os.getenv("MONGO_HOST", "localhost")
MONGO_PORT: int = int(os.getenv("MONGO_PORT", "27017"))
MONGO_DB: str = os.getenv("MONGO_DB", "metadata_inventory")

MONGO_URI: str = os.getenv(
    "MONGO_URI",
    f"mongodb://{MONGO_HOST}:{MONGO_PORT}",
)


# lru_cache makes sure we only create one MongoClient ever
@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    """One shared Mongo connection for the whole app."""
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5000,  # don't hang forever if Mongo is down
    )
    return client


def get_db() -> Database:
    """FastAPI calls this to get the database handle."""
    return get_client()[MONGO_DB]


def get_collection(db: Database | None = None) -> Collection:
    """Get the metadata collection + make sure the url index exists."""
    if db is None:
        db = get_db()
    collection = db["metadata"]
    # unique index on url — so the same URL can't appear twice
    collection.create_index("url", unique=True, background=True)
    return collection


# Called from main.py on startup/shutdown

def ping() -> bool:
    """Quick check — is Mongo alive?"""
    try:
        get_client().admin.command("ping")
        return True
    except Exception:
        return False


def close_connection() -> None:
    """Clean up when the app shuts down."""
    get_client().close()
    get_client.cache_clear()  # clear the cache so next startup gets a fresh client
