"""Local SQLite store for wallets and saved Etherscan API keys.

API keys never leave this computer except in requests to Etherscan.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from tracker.cache import merge_spans, row_identity
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
                enabled INTEGER NOT NULL DEFAULT 1,
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
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_coverage (
                wallet TEXT NOT NULL,
                kind TEXT NOT NULL,
                start_block INTEGER NOT NULL,
                end_block INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cached_rows (
                wallet TEXT NOT NULL,
                kind TEXT NOT NULL,
                block INTEGER NOT NULL,
                row_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (wallet, kind, row_key)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cached_rows_span ON cached_rows (wallet, kind, block)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_coverage_wallet ON scan_coverage (wallet, kind)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_tokens (
                address TEXT PRIMARY KEY,
                symbol TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._ensure_enabled_column()
        self._ensure_wallet_enabled()
        self._migrate_legacy_key()
        self._conn.commit()

    def wallets(self) -> list[dict[str, str | bool]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT address, label, enabled FROM wallets
                ORDER BY enabled DESC, COALESCE(NULLIF(label, ''), address) COLLATE NOCASE
                """
            ).fetchall()
        return [self._public_wallet(row) for row in rows]

    def get_wallet(self, address: str) -> dict[str, str | bool] | None:
        key = normalize_address(address)
        with self._lock:
            row = self._conn.execute(
                "SELECT address, label, enabled FROM wallets WHERE address = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return self._public_wallet(row)

    def save_wallet(self, address: str, label: str) -> dict[str, str | bool]:
        key = normalize_address(address)
        text = clean_label(label)
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute(
                "SELECT enabled FROM wallets WHERE address = ?", (key,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE wallets SET label = ?, enabled = 1, updated_at = ? WHERE address = ?",
                    (text, now, key),
                )
                enabled = True
            else:
                self._conn.execute(
                    """
                    INSERT INTO wallets (address, label, enabled, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    """,
                    (key, text, now, now),
                )
                enabled = True
            self._conn.commit()
        return {"address": key, "label": text, "enabled": enabled}

    def set_wallet_enabled(self, address: str, enabled: bool) -> dict[str, str | bool] | None:
        key = normalize_address(address)
        now = int(time.time())
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE wallets SET enabled = ?, updated_at = ? WHERE address = ?",
                (1 if enabled else 0, now, key),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT address, label, enabled FROM wallets WHERE address = ?", (key,)
            ).fetchone()
        return self._public_wallet(row)

    @staticmethod
    def _public_wallet(row: sqlite3.Row) -> dict[str, str | bool]:
        return {
            "address": row["address"],
            "label": row["label"],
            "enabled": bool(row["enabled"]),
        }

    def delete_wallet(self, address: str) -> bool:
        key = normalize_address(address)
        with self._lock:
            cursor = self._conn.execute("DELETE FROM wallets WHERE address = ?", (key,))
            self._conn.commit()
            return cursor.rowcount > 0

    def label_map(self) -> dict[str, str]:
        return {row["address"]: row["label"] for row in self.wallets() if row["label"]}

    def notify_tokens(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT address, symbol, name, label FROM notify_tokens
                ORDER BY COALESCE(NULLIF(label, ''), NULLIF(symbol, ''), NULLIF(name, ''), address)
                COLLATE NOCASE
                """
            ).fetchall()
        return [
            {
                "address": row["address"],
                "symbol": row["symbol"],
                "name": row["name"],
                "label": row["label"],
            }
            for row in rows
        ]

    def save_notify_token(
        self,
        address: str,
        symbol: str = "",
        name: str = "",
        label: str | None = None,
    ) -> dict[str, str]:
        try:
            key = normalize_address(address)
        except ValueError:
            raise ValueError("Enter a token contract (0x and 40 hex characters).") from None
        symbol_text = clean_label(symbol)
        name_text = clean_label(name)
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute(
                "SELECT symbol, name, label FROM notify_tokens WHERE address = ?",
                (key,),
            ).fetchone()
            if existing:
                kept_symbol = symbol_text or existing["symbol"]
                kept_name = name_text or existing["name"]
                kept_label = existing["label"] if label is None else clean_label(label)
                self._conn.execute(
                    """
                    UPDATE notify_tokens
                    SET symbol = ?, name = ?, label = ?, updated_at = ?
                    WHERE address = ?
                    """,
                    (kept_symbol, kept_name, kept_label, now, key),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO notify_tokens
                    (address, symbol, name, label, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, symbol_text, name_text, clean_label(label or ""), now, now),
                )
            self._conn.commit()
            return {
                "address": key,
                "symbol": symbol_text or (existing["symbol"] if existing else ""),
                "name": name_text or (existing["name"] if existing else ""),
                "label": (existing["label"] if existing and label is None else clean_label(label or "")),
            }

    def delete_notify_token(self, address: str) -> bool:
        key = normalize_address(address)
        with self._lock:
            cursor = self._conn.execute("DELETE FROM notify_tokens WHERE address = ?", (key,))
            self._conn.commit()
            return cursor.rowcount > 0

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

    def _ensure_wallet_enabled(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(wallets)")}
        if "enabled" in columns:
            return
        self._conn.execute("ALTER TABLE wallets ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        self._conn.execute("UPDATE wallets SET enabled = 1")

    def _ensure_enabled_column(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(api_keys)")}
        if "enabled" in columns:
            return
        self._conn.execute("ALTER TABLE api_keys ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        self._conn.execute("UPDATE api_keys SET enabled = 1")

    def _migrate_legacy_key(self) -> None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = 'api_key'").fetchone()
        if row is None or not row["value"]:
            return
        secret = row["value"]
        existing = self._conn.execute("SELECT id FROM api_keys WHERE key = ?", (secret,)).fetchone()
        now = int(time.time())
        if existing is None:
            self._conn.execute(
                "INSERT INTO api_keys (label, key, enabled, created_at, updated_at) VALUES ('', ?, 1, ?, ?)",
                (secret, now, now),
            )
        self._conn.execute("DELETE FROM settings WHERE key = 'api_key'")

    def api_keys(self) -> list[dict[str, str | int | bool]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, label, key, enabled FROM api_keys
                ORDER BY enabled DESC, COALESCE(NULLIF(label, ''), key) COLLATE NOCASE
                """
            ).fetchall()
        return [self._public_key(row) for row in rows]

    def save_api_key(self, key: str, label: str) -> dict[str, str | int | bool]:
        secret = clean_api_key(key)
        text = clean_label(label)
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute("SELECT id FROM api_keys WHERE key = ?", (secret,)).fetchone()
            if existing:
                key_id = existing["id"]
                self._conn.execute(
                    "UPDATE api_keys SET label = ?, enabled = 1, updated_at = ? WHERE id = ?",
                    (text, now, key_id),
                )
            else:
                cursor = self._conn.execute(
                    "INSERT INTO api_keys (label, key, enabled, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                    (text, secret, now, now),
                )
                key_id = int(cursor.lastrowid)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, label, key, enabled FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return self._public_key(row)

    def set_api_key_enabled(self, key_id: int, enabled: bool) -> dict[str, str | int | bool] | None:
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute("SELECT id FROM api_keys WHERE id = ?", (key_id,)).fetchone()
            if existing is None:
                return None
            self._conn.execute(
                "UPDATE api_keys SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, now, key_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, label, key, enabled FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return self._public_key(row)

    def enabled_api_keys(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM api_keys WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        return [row["key"] for row in rows]

    def delete_api_key(self, key_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def clear_active_api_key(self) -> bool:
        with self._lock:
            cursor = self._conn.execute("UPDATE api_keys SET enabled = 0 WHERE enabled = 1")
            self._conn.commit()
            return cursor.rowcount > 0

    def api_key(self) -> str:
        keys = self.enabled_api_keys()
        return keys[0] if keys else ""

    @staticmethod
    def _public_key(row: sqlite3.Row) -> dict[str, str | int | bool]:
        return {
            "id": row["id"],
            "label": row["label"],
            "hint": mask_secret(row["key"]),
            "enabled": bool(row["enabled"]),
        }

    def coverage_spans(self, wallet: str, kind: str) -> list[tuple[int, int]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT start_block, end_block FROM scan_coverage
                WHERE wallet = ? AND kind = ?
                """,
                (wallet, kind),
            ).fetchall()
        return merge_spans([(row["start_block"], row["end_block"]) for row in rows])

    def add_coverage(self, wallet: str, kind: str, start: int, end: int) -> None:
        if start > end:
            return
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT start_block, end_block FROM scan_coverage
                WHERE wallet = ? AND kind = ?
                """,
                (wallet, kind),
            ).fetchall()
            merged = merge_spans(
                [(row["start_block"], row["end_block"]) for row in rows] + [(start, end)]
            )
            self._conn.execute(
                "DELETE FROM scan_coverage WHERE wallet = ? AND kind = ?",
                (wallet, kind),
            )
            self._conn.executemany(
                """
                INSERT INTO scan_coverage (wallet, kind, start_block, end_block)
                VALUES (?, ?, ?, ?)
                """,
                [(wallet, kind, span_start, span_end) for span_start, span_end in merged],
            )
            self._conn.commit()

    def replace_cached_rows(
        self,
        wallet: str,
        kind: str,
        start: int,
        end: int,
        rows: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM cached_rows
                WHERE wallet = ? AND kind = ? AND block BETWEEN ? AND ?
                """,
                (wallet, kind, start, end),
            )
            payload: list[tuple[str, str, int, str, str]] = []
            seen: set[str] = set()
            for row in rows:
                block, key = row_identity(kind, row)
                if block < start or block > end or not key or key in seen:
                    continue
                seen.add(key)
                payload.append(
                    (wallet, kind, block, key, json.dumps(row, separators=(",", ":")))
                )
            if payload:
                self._conn.executemany(
                    """
                    INSERT INTO cached_rows (wallet, kind, block, row_key, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    payload,
                )
            self._conn.commit()

    def load_cached_rows(self, wallet: str, kind: str, start: int, end: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload FROM cached_rows
                WHERE wallet = ? AND kind = ? AND block BETWEEN ? AND ?
                ORDER BY block, row_key
                """,
                (wallet, kind, start, end),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

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
