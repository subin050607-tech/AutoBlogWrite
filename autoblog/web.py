"""로컬 웹 UI + JSON API (표준 라이브러리만 사용).

보안: 127.0.0.1 에만 바인딩하고, Host 헤더 검증(DNS rebinding 방어)과 JSON Content-Type 강제(CSRF 방어)를 한다.
인증이 없으므로 외부에 노출하지 마세요.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import mimetypes
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import workflow
from .db import DB
from .generator import make_generator
from .publishers import make_publisher
from .render import render_html, render_theme

log = logging.getLogger("autoblog")
STATIC = Path(__file__).parent / "static"
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class App:
    """요청 처리 로직. HTTP 와 분리해 두어 테스트하기 쉽다."""

    def __init__(self, cfg: dict, db: DB, generator=None, publisher_factory=None):
        self.cfg, self.db = cfg, db
        self._gen = generator
        self.publisher_factory = publisher_factory or (lambda: make_publisher(cfg))
        self.lock = threading.RLock()

    @property
    def gen(self):
        if self._gen is None:
            self._gen = make_generator(self.cfg)
        return self._gen

    # ---- helpers ----
    def _images_dir(self, topic_id: int) -> Path:
        return Path(self.cfg["paths"]["images"]) / str(int(topic_id))

    def _article(self, aid: int) -> dict:
        a = self.db.get_article(aid)
        if not a:
            raise ApiError("글을 찾을 수 없습니다.", 404)
        return a

    def _topic(self, tid: int) -> dict:
        t = self.db.get_topic(tid)
        if not t:
            raise ApiError("주제를 찾을 수 없습니다.", 404)
        return t

    # ---- dispatch ----
    def handle(self, method: str, path: str, query: dict, body: dict):
        with self.lock:
            try:
                return self._route(method, path, query, body)
            except workflow.WorkflowError as e:
                raise ApiError(str(e), 409)

    def _route(self, method, path, q, body):
        db = self.db
        parts = [p for p in path.split("/") if p][1:]  # 'api' 제거
        m = (method, *parts[:1])
        n = len(parts)
        i = lambda k: int(parts[k])

        if parts == ["dashboard"]:
            return db.dashboard()
        if parts == ["settings"]:
            c = self.cfg
            return {"llm": {k: c["llm"][k] for k in ("provider", "model")}, "blog": {
                "publisher": c["blog"]["publisher"], "publish": c["blog"]["publish"]},
                "persona": c["persona"], "schedule": c["schedule"], "quality": c["quality"]}

        # 키워드
        if parts == ["topics"]:
            if method == "GET":
                return db.list_topics(q.get("status"))
            meta = {k: body[k] for k in ("category", "tone", "notes", "group") if body.get(k)}
            tid = db.add_topic(body.get("keyword", ""), meta, int(body.get("priority") or 0))
            if not tid:
                raise ApiError("이미 등록된 키워드이거나 비어 있습니다.", 409)
            return {"id": tid}
        if n >= 2 and parts[0] == "topics":
            tid, act = i(1), parts[2] if n > 2 else ""
            t = self._topic(tid)
            if n == 2 and method == "PUT":
                meta = {**t["meta"], **{k: body[k] for k in ("category", "tone", "notes", "group") if k in body}}
                db.update_topic(tid, body.get("keyword"), meta, body.get("priority"))
                return {"ok": True}
            if n == 2 and method == "DELETE":
                db.delete_topic(tid)
                return {"ok": True}
            if act == "titles":
                return {"titles": self.gen.suggest_titles(t["keyword"], int(body.get("n") or 5))}
            if act == "outline":
                return {"outline": self.gen.suggest_outline(t["keyword"], body.get("title", ""))}
            if act == "draft":
                aid = workflow.create_draft(db, self.cfg, self.gen, tid, body.get("title", ""), body.get("outline") or [])
                return {"article_id": aid}
            if act == "images":
                d = self._images_dir(tid)
                return {"images": sorted(p.name for p in d.glob("*") if p.suffix.lower() in IMG_EXT)} if d.exists() else {"images": []}
            if act == "upload" and method == "POST":
                return self._upload(tid, body)

        # 글
        if parts == ["articles"]:
            return db.list_articles(q.get("status"))
        if n >= 2 and parts[0] == "articles":
            aid, act = i(1), parts[2] if n > 2 else ""
            a = self._article(aid)
            if n == 2 and method == "GET":
                return a
            if n == 2 and method == "PUT":
                f = {k: body[k] for k in ("title", "body_md", "tags", "summary", "outline") if k in body}
                if "tags" in f and isinstance(f["tags"], str):
                    f["tags"] = [t.strip().lstrip("#") for t in re.split(r"[,\n]", f["tags"]) if t.strip()]
                return {"issues": workflow.save_edit(db, self.cfg, aid, **f)}
            if n == 2 and method == "DELETE":
                if a["status"] == "scheduled":
                    raise ApiError("예약을 먼저 취소하세요.", 409)
                db.delete_article(aid)
                return {"ok": True}
            if method == "POST":
                if act == "review":
                    return {"issues": workflow.review(db, self.cfg, aid)}
                if act == "regenerate":
                    return {"issues": workflow.regenerate(db, self.cfg, self.gen, aid)}
                if act == "schedule":
                    workflow.schedule(db, aid, self._parse_when(body.get("at")))
                    return {"ok": True}
                if act == "cancel":
                    workflow.cancel_schedule(db, aid)
                    return {"ok": True}
                if act == "publish":
                    return {"url": workflow.publish(db, self.cfg, self.publisher_factory(), aid)}
                if act == "copy":
                    return {"id": db.add_article(a["topic_id"], a["title"] + " (복사)", a["body_md"], a["tags"], a["summary"], a["outline"])}
            if act == "preview":
                return ("html", self._preview(a))

        if parts == ["posts"]:
            return db.list_posts(int(q.get("limit", 100)))
        if parts == ["export", "posts.csv"]:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["published_at", "mode", "title", "url", "file_path"])
            for p in db.list_posts(100000):
                w.writerow([p["published_at"], p["mode"], p["title"], p["url"], p["file_path"]])
            return ("csv", "﻿" + buf.getvalue())  # BOM: Excel 한글 깨짐 방지
        raise ApiError("없는 API 입니다.", 404)

    @staticmethod
    def _parse_when(v) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            raise ApiError("예약 시각 형식이 올바르지 않습니다.")

    def _upload(self, tid: int, body: dict) -> dict:
        name = Path(str(body.get("filename", ""))).name
        if Path(name).suffix.lower() not in IMG_EXT or not re.fullmatch(r"[\w가-힣.\- ]+", name):
            raise ApiError("png/jpg/gif/webp 이미지만, 안전한 파일명으로 업로드할 수 있습니다.")
        try:
            data = base64.b64decode(body.get("data", ""), validate=True)
        except Exception:
            raise ApiError("이미지 데이터가 올바르지 않습니다.")
        if not data or len(data) > MAX_UPLOAD:
            raise ApiError("이미지가 비었거나 10MB 를 넘습니다.")
        d = self._images_dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        return {"filename": name}

    def _preview(self, a: dict) -> str:
        tid = a["topic_id"] or 0

        def resolve(src: str):
            if src.startswith(("http://", "https://")):
                return src
            return f"/img/{tid}/{Path(src).name}" if (self._images_dir(tid) / Path(src).name).exists() else None

        body = render_html(a["body_md"], resolve_image=resolve, tags=a["tags"], disclosure="", theme=render_theme(self.cfg))
        from html import escape
        return f'<!doctype html><meta charset="utf-8"><title>{escape(a["title"])}</title><h1>{escape(a["title"])}</h1>{body}'

    def image_file(self, tid: str, name: str) -> Path | None:
        if not tid.isdigit():
            return None
        p = self._images_dir(int(tid)) / Path(name).name
        return p if p.is_file() and p.suffix.lower() in IMG_EXT else None


def make_handler(app: App, port: int):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str, app_page: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            # 앱 페이지만 인라인 스크립트 허용. 미리보기(사용자 생성 HTML)·API 응답은 스크립트 전면 차단
            script = "script-src 'self' 'unsafe-inline'; " if app_page else "script-src 'none'; "
            self.send_header("Content-Security-Policy", f"default-src 'self'; {script}img-src 'self' data: https:; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") \
                else (self.headers.get("Host") or "").split("]")[0] + "]"
            return host in ALLOWED_HOSTS

        def _serve(self, method: str):
            if not self._host_ok():
                return self._json(403, {"error": "허용되지 않은 Host"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if method == "GET" and u.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8", app_page=True)
                if method == "GET" and u.path.startswith("/img/"):
                    seg = u.path.split("/")
                    f = app.image_file(seg[2], "/".join(seg[3:])) if len(seg) >= 4 else None
                    if not f:
                        return self._json(404, {"error": "이미지 없음"})
                    return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                if not u.path.startswith("/api/"):
                    return self._json(404, {"error": "not found"})
                body = {}
                if method in ("POST", "PUT"):
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        return self._json(415, {"error": "Content-Type: application/json 필요"})
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 15 * 1024 * 1024:
                        return self._json(413, {"error": "요청이 너무 큽니다."})
                    body = json.loads(self.rfile.read(n) or b"{}")
                res = app.handle(method, u.path, q, body)
                if isinstance(res, tuple):
                    kind, text = res
                    ctype = {"html": "text/html; charset=utf-8", "csv": "text/csv; charset=utf-8"}[kind]
                    return self._send(200, text.encode("utf-8"), ctype)
                self._json(200, res)
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except json.JSONDecodeError:
                self._json(400, {"error": "JSON 형식 오류"})
            except Exception as e:
                log.exception("web 오류")
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        do_GET = lambda self: self._serve("GET")
        do_POST = lambda self: self._serve("POST")
        do_PUT = lambda self: self._serve("PUT")
        do_DELETE = lambda self: self._serve("DELETE")

    return H


def serve(cfg: dict, db: DB, port: int = 8765, with_scheduler: bool = False) -> None:
    app = App(cfg, db)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    if with_scheduler:
        def loop():
            import time
            while True:
                with app.lock:
                    try:  # 예약 글만 처리한다(주제 큐 자동 발행은 CLI `run` 의 몫)
                        workflow.publish_due(db, cfg, app.publisher_factory)
                    except Exception:
                        log.exception("예약 발행 루프 오류")
                time.sleep(cfg["schedule"]["poll_seconds"])
        threading.Thread(target=loop, daemon=True).start()
    log.info("웹 UI: http://127.0.0.1:%d  (Ctrl+C 로 종료)", port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
