"""로컬 웹 서버 + JSON API (표준 라이브러리만 사용).

127.0.0.1 에만 열고, Host 헤더 검증(DNS 리바인딩 방어)과 JSON Content-Type 강제(CSRF 방어)를 한다.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analysis, naver
from .settings import Settings
from .store import Store

log = logging.getLogger("blogscope")
STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
BLOG_TTL, KEYWORD_TTL = 3600, 6 * 3600


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class App:
    def __init__(self, settings: Settings, store: Store, rss_fetcher=None, post_fetcher=None,
                 api_factory=None, ad_factory=None):
        self.settings, self.store = settings, store
        self.fetch_rss = rss_fetcher or naver.fetch_rss
        self.fetch_post = post_fetcher or naver.fetch_post
        self._api_factory = api_factory or (lambda s: naver.OpenAPI(s.get("naver_client_id"), s.get("naver_client_secret")))
        self._ad_factory = ad_factory or (lambda s: naver.SearchAd(
            s.get("searchad_api_key"), s.get("searchad_secret"), s.get("searchad_customer_id")))

    def api(self, required: bool = False):
        if self.settings.public()["has_search"]:
            return self._api_factory(self.settings)
        if required:
            raise ApiError("이 기능은 네이버 검색 API 키가 필요합니다. '설정' 탭에서 무료 키를 입력하세요.", 412)
        return None

    def ad(self):
        return self._ad_factory(self.settings) if self.settings.public()["has_searchad"] else None

    # ------------------------------------------------------------ routes
    def handle(self, method: str, path: str, q: dict, body: dict):
        try:
            return self._route(method, path, q, body)
        except ValueError as e:
            raise ApiError(str(e))
        except naver.NaverError as e:
            raise ApiError(str(e), 502 if e.status in (0, 429) or e.status >= 500 else 400)

    def _route(self, method, path, q, body):
        if path == "/api/settings":
            if method == "POST":
                self.settings.update(body)
                self.store.cache_clear()
            return self.settings.public()

        if path == "/api/blog" and method == "POST":
            blog_id, _ = naver.parse_blog_ref(body.get("blog", ""))
            ck = f"blog:{blog_id}:{self.settings.public()['has_search']}"
            if not body.get("refresh") and (hit := self.store.cache_get(ck, BLOG_TTL)):
                return {**hit, "cached": True}
            rep = analysis.analyze_blog(self.fetch_rss(blog_id), self.api())
            self.store.cache_set(ck, rep)
            self.store.save("blog", blog_id, rep["blog"]["title"], rep["score"], rep)
            rep["history"] = self.store.history("blog", blog_id)
            return rep

        if path == "/api/keyword" and method == "POST":
            kw = (body.get("keyword") or "").strip()
            pub = self.settings.public()
            ck = f"kw:{kw}:{pub['has_search']}:{pub['has_searchad']}"
            if not body.get("refresh") and (hit := self.store.cache_get(ck, KEYWORD_TTL)):
                return {**hit, "cached": True}
            api, ad = self.api(), self.ad()
            if not api and not ad:
                raise ApiError("키워드 분석은 네이버 API 키가 필요합니다. '설정' 탭에서 무료 키를 입력하세요.", 412)
            rep = analysis.analyze_keyword(kw, api, ad)
            self.store.cache_set(ck, rep)
            self.store.save("keyword", kw, kw, rep["volume"]["total"] if rep["volume"] else None, rep)
            return rep

        if path == "/api/rank" and method == "POST":
            blog_id, _ = naver.parse_blog_ref(body.get("blog", ""))
            kws = body.get("keywords") or []
            if isinstance(kws, str):
                kws = kws.replace(",", "\n").split("\n")
            if not any(k.strip() for k in kws):
                raise ApiError("확인할 키워드를 입력하세요.")
            return {"blog_id": blog_id, "results": analysis.rank_check(self.api(required=True), blog_id, kws)}

        if path == "/api/post" and method == "POST":
            title, text, images, src = body.get("title", ""), body.get("text", ""), None, "paste"
            if body.get("url"):
                blog_id, log_no = naver.parse_blog_ref(body["url"])
                if not log_no:
                    raise ApiError("글 주소에 글 번호가 없습니다. 예: https://blog.naver.com/아이디/224410368545")
                p = self.fetch_post(blog_id, log_no)
                title, text, images, src = p["title"], p["text"], p["images"], "url"
            rep = analysis.diagnose_post(title, text, body.get("keyword", ""), images)
            rep.update(source=src, title=title)
            return rep

        if path == "/api/compare" and method == "POST":
            refs = [b for b in (body.get("blogs") or []) if str(b).strip()][:3]
            if len(refs) < 2:
                raise ApiError("비교할 블로그를 2개 이상 입력하세요.")
            api, out = self.api(), []
            for r in refs:
                bid, _ = naver.parse_blog_ref(r)
                try:
                    rep = analysis.analyze_blog(self.fetch_rss(bid), api, exposure_limit=5)
                    out.append({"blog_id": bid, "title": rep["blog"]["title"], "score": rep["score"],
                                "grade": rep["grade"], "name": rep["name"], "components": rep["components"],
                                "metrics": {k: rep["metrics"][k] for k in ("recent7", "recent30", "last_post_days",
                                                                         "avg_interval_days", "posts_in_rss")}})
                except naver.NaverError as e:
                    out.append({"blog_id": bid, "error": str(e)})
            return {"blogs": out}

        if path == "/api/history":
            kind = q.get("kind", "blog")
            if kind not in ("blog", "keyword"):
                raise ApiError("kind 는 blog 또는 keyword")
            if method == "DELETE":
                self.store.delete(kind, q.get("key", ""))
                return {"ok": True}
            if q.get("key"):
                return {"history": self.store.history(kind, q["key"]), "latest": self.store.latest(kind, q["key"])}
            return self.store.recent(kind)
        raise ApiError("없는 API 입니다.", 404)


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str, page: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            script = "'self' 'unsafe-inline'" if page else "'none'"
            self.send_header("Content-Security-Policy",
                             f"default-src 'self'; script-src {script}; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _serve(self, method: str):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ALLOWED_HOSTS:
                return self._json(403, {"error": "허용되지 않은 Host"})
            u = urlparse(self.path)
            try:
                if method == "GET" and u.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8", page=True)
                if u.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if not u.path.startswith("/api/"):
                    return self._json(404, {"error": "not found"})
                body = {}
                if method == "POST":
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        return self._json(415, {"error": "Content-Type: application/json 필요"})
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 2 * 1024 * 1024:
                        return self._json(413, {"error": "요청이 너무 큽니다."})
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(body, dict):
                        return self._json(400, {"error": "JSON 객체가 필요합니다."})
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                self._json(200, app.handle(method, u.path, q, body))
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except json.JSONDecodeError:
                self._json(400, {"error": "JSON 형식 오류"})
            except Exception as e:
                log.exception("서버 오류")
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_DELETE(self):
            self._serve("DELETE")

    return H


def serve(data_dir: str = "data", port: int = 8765, open_browser: bool = True) -> None:
    app = App(Settings(Path(data_dir) / "settings.json"), Store(Path(data_dir) / "blogscope.db"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}"
    print(f"BlogScope 실행 중: {url}  (종료: Ctrl+C)")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
