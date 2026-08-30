from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("scanner_cache.sqlite3")
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    return conn


def get_cached_product(product_id: str, region: str) -> dict[str, Any] | None:
    key = f"{region}:{product_id}"
    with _conn() as conn:
        row = conn.execute(
            "SELECT payload, updated_at FROM product_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    payload, updated_at = row
    if int(time.time()) - int(updated_at) > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def set_cached_product(product_id: str, region: str, payload: dict[str, Any]) -> None:
    key = f"{region}:{product_id}"
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO product_cache(cache_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(payload), int(time.time())),
        )
