"""로컬 웹 서버 + JSON API (표준 라이브러리만 사용).

127.0.0.1 에만 열고, Host 헤더 검증(DNS 리바인딩 방어)과 JSON Content-Type 강제(CSRF 방어)를 한다.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import shutil

from . import analysis, images, llm, naver, writer
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
                 api_factory=None, ad_factory=None, llm_factory=None, image_factory=None, images_dir=None):
        self.settings, self.store = settings, store
        self.images_dir = Path(images_dir or "data/images")
        self._image_factory = image_factory or self._default_image
        self._llm_factory = llm_factory or self._default_llm
        self.fetch_rss = rss_fetcher or naver.fetch_rss
        self.fetch_post = post_fetcher or naver.fetch_post
        self._api_factory = api_factory or (lambda s: naver.OpenAPI(
            s.get("naver_client_id"), s.get("naver_client_secret"), hub=s.get("naver_api_source") != "developers"))
        self._ad_factory = ad_factory or (lambda s: naver.SearchAd(
            s.get("searchad_api_key"), s.get("searchad_secret"), s.get("searchad_customer_id")))

    @staticmethod
    def _default_llm(s: Settings):
        if s.get("llm_provider") == "openai_compat":
            return llm.OpenAICompat(s.get("llm_base_url"), s.get("llm_api_key"), s.get("llm_model"))
        return llm.Gemini(s.get("gemini_api_key"), s.get("llm_model") or llm.DEFAULT_GEMINI_MODEL)

    @staticmethod
    def _default_image(s: Settings):
        if s.get("image_provider") == "pollinations":
            return images.Pollinations()
        return images.GeminiImage(s.get("gemini_api_key"), s.get("image_model"))

    def _inp(self, raw) -> dict:
        """원고 입력 + 설정의 글 스타일 정보(닉네임·인사말·예시 글)."""
        raw = dict(raw or {})
        for k in ("nickname", "signature", "style_example"):
            raw.setdefault(k, self.settings.get(k))
        return writer.normalize(raw)

    def llm(self):
        if not self.settings.public()["has_llm"]:
            raise ApiError("글쓰기는 AI 키가 필요합니다. '설정' 탭에서 무료 Gemini API 키를 입력하세요.", 412)
        return self._llm_factory(self.settings)

    def _remember_model(self, client) -> str:
        """구글 안내로 모델이 자동 전환됐으면 설정에 저장. 혼잡해서 임시 대체 모델을 썼으면 안내 문구를 돌려준다."""
        new = getattr(client, "switched_to", None)
        if new:
            self.settings.update({"llm_model": new})
        used = getattr(client, "used_model", None)
        return f"기본 모델이 혼잡해서 이번에는 '{used}' 모델로 작성했습니다." if used else ""

    def _refs(self, keyword: str) -> list[dict]:
        api = self.api()
        if not api:
            return []
        ck = f"refs:{keyword}"
        if (hit := self.store.cache_get(ck, KEYWORD_TTL)) is not None:
            return hit
        try:
            refs = [{"title": it["title"], "description": it["description"], "link": it["link"], "blogger": it["blogger"]}
                    for it in api.search_blog(keyword, display=10, sort="sim")["items"]]
        except naver.NaverError:
            return []
        self.store.cache_set(ck, refs)
        return refs

    def _volume(self, keyword: str):
        ad = self.ad()
        if not ad:
            return None
        try:
            rel = ad.keywords([keyword])
        except naver.NaverError:
            return None
        k = re.sub(r"\s+", "", keyword).lower()
        return next((x for x in rel if re.sub(r"\s+", "", x["keyword"]).lower() == k), None)

    def _finish(self, doc_id: int | None, inp: dict, plan: dict, doc: dict, save: bool = True) -> dict:
        marks = writer.count_photo_marks(doc) if inp.get("photo_marks") else None
        diag = analysis.diagnose_post(doc["title"], writer.to_text(doc), inp["keyword"], marks)
        if save:
            doc_id = self.store.doc_save(doc_id, inp["keyword"], doc["title"], inp, plan, doc)
        return {"id": doc_id, "input": inp, "plan": plan, "doc": doc, "html": writer.to_html(doc),
                "text": writer.to_text(doc), "diagnosis": diag}

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
        except llm.LLMError as e:
            raise ApiError(str(e), e.status if e.status in (400, 401, 403, 404, 412, 429, 503) else 502)
        except images.ImageError as e:
            raise ApiError(str(e), e.status if e.status in (400, 401, 403, 404, 412, 429, 503) else 502)

    def _route(self, method, path, q, body):
        if path == "/api/settings":
            if method == "POST":
                self.settings.update(body)
                self.store.cache_clear()
            return self.settings.public()

        # ------------------------------------------------ 글쓰기
        if path == "/api/writer/options":
            return {**writer.options(), "image": images.options()}
        if path == "/api/images":
            doc_id = int(q.get("doc_id") or body.get("doc_id") or 0)
            if not self.store.doc_get(doc_id):
                raise ApiError("원고를 찾을 수 없습니다.", 404)
            if method == "DELETE":
                images.delete(self.images_dir, doc_id, q.get("name", ""))
                return {"ok": True}
            if method == "POST":
                desc = (body.get("desc") or "").strip()
                if not desc:
                    raise ApiError("어떤 이미지를 만들지 설명을 입력하세요.")
                # 이미지 모델은 영어를 훨씬 잘 이해하므로, 직접 쓴 영어가 없으면 AI로 글 맥락을 넣어 영어 설명을 만든다
                english = (body.get("prompt_en") or "").strip()[:600]
                if not english and self.settings.public()["has_llm"]:
                    d = self.store.doc_get(doc_id)
                    try:
                        english = images.to_english(self.llm(), desc, d["doc"].get("title", ""), d["keyword"])
                    except ApiError:
                        english = ""
                aspect = body.get("aspect", "1:1")
                prompt = images.build_prompt(english or desc, body.get("style", "illust"), aspect)
                data, mime = self._image_factory(self.settings).generate(prompt, aspect)
                name = images.save(self.images_dir, doc_id, data, mime)
                return {"name": name, "url": f"/img/{doc_id}/{name}", "prompt_en": english}
            return [{"name": n, "url": f"/img/{doc_id}/{n}"} for n in images.list_images(self.images_dir, doc_id)]
        if path == "/api/writer/plan" and method == "POST":
            inp = self._inp(body.get("input"))
            client = self.llm()
            refs = self._refs(inp["keyword"]) if body.get("use_refs", True) else []
            res = writer.plan(client, inp, refs)
            notice = self._remember_model(client)
            return {**res, "input": inp, "refs": refs, "volume": self._volume(inp["keyword"]), "notice": notice}
        if path == "/api/writer/write" and method == "POST":
            inp = self._inp(body.get("input"))
            client = self.llm()
            outline = [o for o in (body.get("outline") or []) if isinstance(o, dict) and str(o.get("heading", "")).strip()]
            refs = self._refs(inp["keyword"]) if body.get("use_refs", True) else []
            doc = writer.write(client, inp, body.get("title", ""), outline, refs)
            notice = self._remember_model(client)
            plan = {"titles": body.get("titles") or [], "outline": outline, "hashtags": body.get("hashtags") or []}
            return {**self._finish(body.get("id"), inp, plan, doc), "notice": notice}
        if path == "/api/writer/rewrite" and method == "POST":
            d = self.store.doc_get(int(body.get("id") or 0))
            if not d:
                raise ApiError("원고를 찾을 수 없습니다.", 404)
            doc = writer.clean_doc(body.get("doc") or d["doc"])  # 화면에서 고친 내용 기준
            part = str(body.get("part", ""))
            client = self.llm()
            new = writer.rewrite_part(client, d["input"], doc, part, body.get("instruction", ""))
            notice = self._remember_model(client)
            if part in ("intro", "outro"):
                doc[part] = new["content"]
            else:
                doc["sections"][int(part)] = new
            return {**self._finish(d["id"], d["input"], d["plan"], doc), "notice": notice}

        if path == "/api/docs":
            return self.store.doc_list()
        if path.startswith("/api/docs/"):
            try:
                doc_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                raise ApiError("잘못된 원고 번호", 400)
            d = self.store.doc_get(doc_id)
            if not d:
                raise ApiError("원고를 찾을 수 없습니다.", 404)
            if method == "DELETE":
                self.store.doc_delete(doc_id)
                shutil.rmtree(self.images_dir / str(doc_id), ignore_errors=True)
                return {"ok": True}
            if method == "POST":  # 화면에서 직접 고친 내용 저장
                return self._finish(doc_id, d["input"], d["plan"], writer.clean_doc(body.get("doc")))
            return self._finish(doc_id, d["input"], d["plan"], d["doc"], save=False)

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
            title, text, n_img, src = body.get("title", ""), body.get("text", ""), None, "paste"
            if body.get("url"):
                blog_id, log_no = naver.parse_blog_ref(body["url"])
                if not log_no:
                    raise ApiError("글 주소에 글 번호가 없습니다. 예: https://blog.naver.com/아이디/224410368545")
                p = self.fetch_post(blog_id, log_no)
                title, text, n_img, src = p["title"], p["text"], p["images"], "url"
            rep = analysis.diagnose_post(title, text, body.get("keyword", ""), n_img)
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
                if method == "GET" and u.path.startswith("/img/"):
                    seg = u.path.split("/")
                    f = images.path_of(app.images_dir, seg[2], seg[3]) if len(seg) == 4 else None
                    if not f:
                        return self._json(404, {"error": "이미지 없음"})
                    mime = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[f.suffix]
                    return self._send(200, f.read_bytes(), mime)
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
    app = App(Settings(Path(data_dir) / "settings.json"), Store(Path(data_dir) / "blogscope.db"),
              images_dir=Path(data_dir) / "images")
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
