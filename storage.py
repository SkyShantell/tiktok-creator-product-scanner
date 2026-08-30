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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creator_groups (
            group_name TEXT NOT NULL,
            creator TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (group_name, creator)
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


def list_creator_groups() -> dict[str, list[str]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT group_name, creator FROM creator_groups ORDER BY lower(group_name), lower(creator)"
        ).fetchall()
    groups: dict[str, list[str]] = {}
    for group_name, creator in rows:
        groups.setdefault(str(group_name), []).append(str(creator))
    return groups


def add_creators_to_group(group_name: str, creators: list[str]) -> int:
    group_name = (group_name or "").strip()
    creators = list(dict.fromkeys(c.strip().lstrip("@") for c in creators if c and c.strip()))
    if not group_name or not creators:
        return 0
    added = 0
    now = int(time.time())
    with _conn() as conn:
        for creator in creators:
            cur = conn.execute(
                "INSERT OR IGNORE INTO creator_groups(group_name, creator, created_at) VALUES (?, ?, ?)",
                (group_name, creator, now),
            )
            added += max(cur.rowcount, 0)
    return added


def remove_creators_from_group(group_name: str, creators: list[str]) -> int:
    creators = [c for c in creators if c]
    if not group_name or not creators:
        return 0
    with _conn() as conn:
        cur = conn.executemany(
            "DELETE FROM creator_groups WHERE group_name = ? AND creator = ?",
            [(group_name, creator) for creator in creators],
        )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def delete_creator_group(group_name: str) -> int:
    if not group_name:
        return 0
    with _conn() as conn:
        cur = conn.execute("DELETE FROM creator_groups WHERE group_name = ?", (group_name,))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def export_creator_groups() -> dict[str, list[str]]:
    return list_creator_groups()


def import_creator_groups(payload: Any, *, replace: bool = False) -> int:
    if not isinstance(payload, dict):
        raise ValueError("Group backup must be a JSON object of group names to creator lists.")
    normalized: dict[str, list[str]] = {}
    for group_name, creators in payload.items():
        if not isinstance(group_name, str) or not isinstance(creators, list):
            continue
        clean_group = group_name.strip()
        clean_creators = [str(c).strip().lstrip("@") for c in creators if str(c).strip()]
        if clean_group and clean_creators:
            normalized[clean_group] = clean_creators
    if replace:
        with _conn() as conn:
            conn.execute("DELETE FROM creator_groups")
    total = 0
    for group_name, creators in normalized.items():
        total += add_creators_to_group(group_name, creators)
    return total
