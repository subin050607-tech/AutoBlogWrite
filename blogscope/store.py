"""SQLite: 분석 기록(점수 추이용) + 결과 캐시(API 호출 절약)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,          -- blog | keyword
  key TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  score REAL,
  at TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(kind, key, at);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, at REAL NOT NULL, data TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def save(self, kind: str, key: str, title: str, score: float | None, data: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO snapshots(kind,key,title,score,at,data) VALUES (?,?,?,?,?,?)",
                              (kind, key, title, score, datetime.now().isoformat(timespec="seconds"),
                               json.dumps(data, ensure_ascii=False)))
            self.conn.commit()

    def recent(self, kind: str, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT key, title, score, MAX(at) AS at, COUNT(*) AS n FROM snapshots WHERE kind=? "
            "GROUP BY key ORDER BY at DESC LIMIT ?", (kind, limit)).fetchall()
        return [dict(r) for r in rows]

    def history(self, kind: str, key: str) -> list[dict]:
        rows = self.conn.execute("SELECT at, score FROM snapshots WHERE kind=? AND key=? ORDER BY at", (kind, key))
        return [dict(r) for r in rows]

    def latest(self, kind: str, key: str) -> dict | None:
        r = self.conn.execute("SELECT data FROM snapshots WHERE kind=? AND key=? ORDER BY at DESC LIMIT 1",
                              (kind, key)).fetchone()
        return json.loads(r["data"]) if r else None

    def delete(self, kind: str, key: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM snapshots WHERE kind=? AND key=?", (kind, key))
            self.conn.commit()

    def cache_get(self, key: str, ttl: float):
        r = self.conn.execute("SELECT at, data FROM cache WHERE key=?", (key,)).fetchone()
        if r and time.time() - r["at"] < ttl:
            return json.loads(r["data"])
        return None

    def cache_set(self, key: str, data) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO cache(key,at,data) VALUES (?,?,?)",
                              (key, time.time(), json.dumps(data, ensure_ascii=False)))
            self.conn.commit()

    def cache_clear(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM cache")
            self.conn.commit()
