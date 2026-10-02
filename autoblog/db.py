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
  status TEXT NOT NULL DEFAULT 'pending', -- pending | drafted | done | failed | skipped
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
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id INTEGER,
  title TEXT NOT NULL,
  body_md TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  outline TEXT NOT NULL DEFAULT '[]',
  issues TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',  -- draft | reviewed | scheduled | published | failed
  scheduled_at TEXT,
  retries INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  post_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

ARTICLE_JSON = ("tags", "outline", "issues")
ARTICLE_FIELDS = {"title", "body_md", "tags", "summary", "outline", "issues", "status",
                  "scheduled_at", "retries", "last_error", "post_id"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)  # 웹 서버/스케줄러 스레드 공유(호출측에서 락)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- topics ----
    def add_topic(self, keyword: str, meta: dict | None = None, priority: int = 0) -> int | None:
        keyword = keyword.strip()
        if not keyword:
            return None
        dup = self.conn.execute(
            "SELECT id FROM topics WHERE keyword=? AND status IN ('pending','drafted','done')", (keyword,)
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

    def update_topic(self, topic_id: int, keyword: str | None = None, meta: dict | None = None,
                     priority: int | None = None) -> None:
        t = self.get_topic(topic_id)
        if not t:
            raise KeyError(f"주제 #{topic_id} 없음")
        self.conn.execute(
            "UPDATE topics SET keyword=?, meta=?, priority=? WHERE id=?",
            ((keyword or t["keyword"]).strip(), json.dumps(meta if meta is not None else t["meta"], ensure_ascii=False),
             t["priority"] if priority is None else priority, topic_id),
        )
        self.conn.commit()

    def delete_topic(self, topic_id: int) -> None:
        self.conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
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

    # ---- articles (초안 -> 검수 -> 예약/발행 워크플로) ----
    def add_article(self, topic_id: int | None, title: str, body_md: str = "", tags: list[str] | None = None,
                    summary: str = "", outline: list[str] | None = None, issues: list[str] | None = None) -> int:
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT INTO articles(topic_id,title,body_md,tags,summary,outline,issues,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (topic_id, title, body_md, json.dumps(tags or [], ensure_ascii=False), summary,
             json.dumps(outline or [], ensure_ascii=False), json.dumps(issues or [], ensure_ascii=False), ts, ts),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_article(self, article_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return self._article(r) if r else None

    def list_articles(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute("SELECT * FROM articles WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM articles ORDER BY id DESC").fetchall()
        return [self._article(r) for r in rows]

    def update_article(self, article_id: int, **fields) -> None:
        bad = set(fields) - ARTICLE_FIELDS
        if bad:
            raise ValueError(f"수정할 수 없는 필드: {sorted(bad)}")
        if not fields:
            return
        vals = [json.dumps(v, ensure_ascii=False) if k in ARTICLE_JSON else v for k, v in fields.items()]
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE articles SET {sets}, updated_at=? WHERE id=?", (*vals, now_iso(), article_id))
        self.conn.commit()

    def delete_article(self, article_id: int) -> None:
        self.conn.execute("DELETE FROM articles WHERE id=?", (article_id,))
        self.conn.commit()

    def due_articles(self, now: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM articles WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at", (now,)
        ).fetchall()
        return [self._article(r) for r in rows]

    @staticmethod
    def _article(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ARTICLE_JSON:
            d[k] = json.loads(d[k] or "[]")
        return d

    def dashboard(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        count = lambda q, *a: self.conn.execute(q, a).fetchone()[0]
        by_status = {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM articles GROUP BY status")}
        return {
            "articles_total": sum(by_status.values()),
            "articles_by_status": by_status,
            "topics_by_status": {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM topics GROUP BY status")},
            "published_today": count("SELECT COUNT(*) FROM posts WHERE published_at>=?", day.isoformat(timespec="seconds")),
            "published_month": count("SELECT COUNT(*) FROM posts WHERE published_at>=?", month.isoformat(timespec="seconds")),
            "upcoming": [dict(r) for r in self.conn.execute(
                "SELECT id,title,scheduled_at FROM articles WHERE status='scheduled' ORDER BY scheduled_at LIMIT 10")],
            "recent_articles": [dict(r) for r in self.conn.execute(
                "SELECT id,title,status,updated_at FROM articles ORDER BY updated_at DESC LIMIT 5")],
            "recent_posts": self.list_posts(5),
        }
