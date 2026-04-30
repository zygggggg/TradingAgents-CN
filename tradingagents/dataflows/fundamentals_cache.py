"""Optional MongoDB cache for validated A-share fundamentals snapshots."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from tradingagents.utils.logging_manager import get_logger


logger = get_logger("dataflows")


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def fundamentals_cache_enabled() -> bool:
    return _truthy_env("FUNDAMENTALS_MONGO_CACHE_ENABLED", _truthy_env("MONGODB_ENABLED", False))


def _mongo_uri() -> str:
    if os.getenv("MONGODB_CONNECTION_STRING"):
        return os.getenv("MONGODB_CONNECTION_STRING", "")
    if os.getenv("MONGO_URI"):
        return os.getenv("MONGO_URI", "")
    host = os.getenv("MONGODB_HOST", "127.0.0.1")
    port = os.getenv("MONGODB_PORT", "27017")
    username = os.getenv("MONGODB_USERNAME")
    password = os.getenv("MONGODB_PASSWORD")
    auth_source = os.getenv("MONGODB_AUTH_SOURCE", "admin")
    if username and password:
        return f"mongodb://{username}:{password}@{host}:{port}/?authSource={auth_source}"
    return f"mongodb://{host}:{port}/"


def _db_name() -> str:
    return os.getenv("MONGODB_DATABASE_NAME") or os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DB") or "tradingagents"


def _collection_name() -> str:
    return os.getenv("FUNDAMENTALS_MONGO_COLLECTION", "stock_fundamentals_snapshots")


def _client():
    from pymongo import MongoClient

    timeout_ms = int(os.getenv("FUNDAMENTALS_MONGO_TIMEOUT_MS", "2500"))
    return MongoClient(_mongo_uri(), serverSelectionTimeoutMS=timeout_ms)


def read_cached_fundamentals(symbol: str, *, max_age_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if not fundamentals_cache_enabled():
        return None
    max_age_days = int(max_age_days if max_age_days is not None else os.getenv("FUNDAMENTALS_MONGO_MAX_AGE_DAYS", "7"))
    try:
        client = _client()
        client.admin.command("ping")
        collection = client[_db_name()][_collection_name()]
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        doc = collection.find_one(
            {"symbol": str(symbol), "quality_ok": True, "updated_at": {"$gte": cutoff}},
            sort=[("updated_at", -1)],
        )
        return doc
    except Exception as exc:
        logger.debug("财务Mongo缓存读取失败: %s", exc)
        return None


def write_cached_fundamentals(symbol: str, text: str, source: str, quality: Dict[str, Any]) -> bool:
    if not fundamentals_cache_enabled():
        return False
    try:
        client = _client()
        client.admin.command("ping")
        collection = client[_db_name()][_collection_name()]
        collection.create_index([("symbol", 1), ("updated_at", -1)])
        collection.create_index([("symbol", 1), ("quality_ok", 1), ("updated_at", -1)])
        now = datetime.now(timezone.utc)
        collection.update_one(
            {"symbol": str(symbol), "source": source, "text_hash": str(hash(text))},
            {
                "$set": {
                    "symbol": str(symbol),
                    "source": source,
                    "text": text,
                    "quality": quality,
                    "quality_ok": bool(quality.get("ok")),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return True
    except Exception as exc:
        logger.debug("财务Mongo缓存写入失败: %s", exc)
        return False
