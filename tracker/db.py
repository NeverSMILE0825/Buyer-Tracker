"""Local SQLite store for wallets and saved Etherscan API keys.

API keys never leave this computer except in requests to Etherscan.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from tracker.checksum import normalize_address
from tracker.formatutil import clean_api_key, clean_label, mask_secret

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "tracker.db"

DEFAULT_RATE = 3.0
DEFAULT_PAGE_SIZE = 1000


class Database:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                key TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._migrate_legacy_key()
        self._conn.commit()

    def wallets(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, label FROM wallets ORDER BY COALESCE(NULLIF(label, ''), address) COLLATE NOCASE"
            ).fetchall()
        return [{"address": row["address"], "label": row["label"]} for row in rows]

    def get_wallet(self, address: str) -> dict[str, str] | None:
        key = normalize_address(address)
        with self._lock:
            row = self._conn.execute(
                "SELECT address, label FROM wallets WHERE address = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return {"address": row["address"], "label": row["label"]}

    def save_wallet(self, address: str, label: str) -> dict[str, str]:
        key = normalize_address(address)
        text = clean_label(label)
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM wallets WHERE address = ?", (key,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE wallets SET label = ?, updated_at = ? WHERE address = ?",
                    (text, now, key),
                )
            else:
                self._conn.execute(
                    "INSERT INTO wallets (address, label, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (key, text, now, now),
                )
            self._conn.commit()
        return {"address": key, "label": text}

    def delete_wallet(self, address: str) -> bool:
        key = normalize_address(address)
        with self._lock:
            cursor = self._conn.execute("DELETE FROM wallets WHERE address = ?", (key,))
            self._conn.commit()
            return cursor.rowcount > 0

    def label_map(self) -> dict[str, str]:
        return {row["address"]: row["label"] for row in self.wallets() if row["label"]}

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def delete_setting(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn.commit()

    def _migrate_legacy_key(self) -> None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = 'api_key'").fetchone()
        if row is None or not row["value"]:
            return
        secret = row["value"]
        existing = self._conn.execute("SELECT id FROM api_keys WHERE key = ?", (secret,)).fetchone()
        now = int(time.time())
        if existing is None:
            self._conn.execute("UPDATE api_keys SET active = 0")
            self._conn.execute(
                "INSERT INTO api_keys (label, key, active, created_at, updated_at) VALUES ('', ?, 1, ?, ?)",
                (secret, now, now),
            )
        self._conn.execute("DELETE FROM settings WHERE key = 'api_key'")

    def api_keys(self) -> list[dict[str, str | int | bool]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, label, key, active FROM api_keys
                ORDER BY active DESC, COALESCE(NULLIF(label, ''), key) COLLATE NOCASE
                """
            ).fetchall()
        return [self._public_key(row) for row in rows]

    def save_api_key(self, key: str, label: str) -> dict[str, str | int | bool]:
        secret = clean_api_key(key)
        text = clean_label(label)
        now = int(time.time())
        with self._lock:
            self._conn.execute("UPDATE api_keys SET active = 0")
            existing = self._conn.execute("SELECT id FROM api_keys WHERE key = ?", (secret,)).fetchone()
            if existing:
                key_id = existing["id"]
                self._conn.execute(
                    "UPDATE api_keys SET label = ?, active = 1, updated_at = ? WHERE id = ?",
                    (text, now, key_id),
                )
            else:
                cursor = self._conn.execute(
                    "INSERT INTO api_keys (label, key, active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                    (text, secret, now, now),
                )
                key_id = int(cursor.lastrowid)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, label, key, active FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return self._public_key(row)

    def activate_api_key(self, key_id: int) -> dict[str, str | int | bool] | None:
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute("SELECT id FROM api_keys WHERE id = ?", (key_id,)).fetchone()
            if existing is None:
                return None
            self._conn.execute("UPDATE api_keys SET active = 0")
            self._conn.execute(
                "UPDATE api_keys SET active = 1, updated_at = ? WHERE id = ?",
                (now, key_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, label, key, active FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return self._public_key(row)

    def delete_api_key(self, key_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def clear_active_api_key(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT id FROM api_keys WHERE active = 1 LIMIT 1").fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM api_keys WHERE id = ?", (row["id"],))
            self._conn.commit()
            return True

    def api_key(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT key FROM api_keys WHERE active = 1 ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return row["key"] if row else ""

    @staticmethod
    def _public_key(row: sqlite3.Row) -> dict[str, str | int | bool]:
        return {
            "id": row["id"],
            "label": row["label"],
            "hint": mask_secret(row["key"]),
            "active": bool(row["active"]),
        }

    def rate(self) -> float:
        raw = self.get_setting("rate", str(DEFAULT_RATE))
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_RATE
        return min(30.0, max(1.0, value))

    def page_size(self) -> int:
        raw = self.get_setting("page_size", str(DEFAULT_PAGE_SIZE))
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_PAGE_SIZE
        return min(10_000, max(100, value))

    def key_hint(self) -> str:
        return mask_secret(self.api_key()) if self.api_key() else ""


db = Database()
