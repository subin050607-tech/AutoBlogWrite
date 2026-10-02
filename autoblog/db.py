"""SQLite: 주제 큐(topics) 와 발행 이력(posts)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  keyword TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',       -- JSON: category, tone, notes, images, ...
  status TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | skipped
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id INTEGER,
  title TEXT NOT NULL,
  remote_id TEXT,
  url TEXT,
  mode TEXT NOT NULL,
  file_path TEXT,
  published_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- topics ----
    def add_topic(self, keyword: str, meta: dict | None = None, priority: int = 0) -> int | None:
        keyword = keyword.strip()
        if not keyword:
            return None
        dup = self.conn.execute(
            "SELECT id FROM topics WHERE keyword=? AND status IN ('pending','done')", (keyword,)
        ).fetchone()
        if dup:
            return None  # 같은 키워드 중복 등록 방지
        cur = self.conn.execute(
            "INSERT INTO topics(keyword, meta, priority, created_at) VALUES (?,?,?,?)",
            (keyword, json.dumps(meta or {}, ensure_ascii=False), priority, now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_topic(self, topic_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        return self._topic(r) if r else None

    def next_topic(self) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM topics WHERE status='pending' ORDER BY priority DESC, id ASC LIMIT 1"
        ).fetchone()
        return self._topic(r) if r else None

    def list_topics(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute("SELECT * FROM topics WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM topics ORDER BY id").fetchall()
        return [self._topic(r) for r in rows]

    def set_status(self, topic_id: int, status: str, error: str | None = None, bump_attempt: bool = False) -> None:
        self.conn.execute(
            "UPDATE topics SET status=?, last_error=?, attempts=attempts+? WHERE id=?",
            (status, error, 1 if bump_attempt else 0, topic_id),
        )
        self.conn.commit()

    @staticmethod
    def _topic(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["meta"] = json.loads(d["meta"] or "{}")
        return d

    # ---- posts ----
    def add_post(self, topic_id: int | None, title: str, mode: str, remote_id: str = "", url: str = "",
                 file_path: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO posts(topic_id,title,remote_id,url,mode,file_path,published_at) VALUES (?,?,?,?,?,?,?)",
            (topic_id, title, remote_id, url, mode, file_path, now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_posts(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def all_titles(self) -> list[str]:
        return [r["title"] for r in self.conn.execute("SELECT title FROM posts")]

    def posts_since(self, since_iso: str, real_only: bool = True) -> list[dict]:
        q = "SELECT * FROM posts WHERE published_at>=?"
        if real_only:
            q += " AND mode='naver'"
        return [dict(r) for r in self.conn.execute(q + " ORDER BY published_at", (since_iso,))]

    def last_post_time(self, real_only: bool = True) -> datetime | None:
        q = "SELECT published_at FROM posts"
        if real_only:
            q += " WHERE mode='naver'"
        r = self.conn.execute(q + " ORDER BY published_at DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(r[0]) if r else None
