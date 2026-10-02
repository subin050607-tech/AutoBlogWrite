import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from blogscope import images, llm, writer
from blogscope.settings import Settings
from blogscope.store import Store
from blogscope.web import App, ApiError, make_handler
from fakes import FakeLLM

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


# ------------------------------------------------------------ 글 스타일
def test_magazine_style_prompt_and_fields():
    f = FakeLLM()
    inp = {"keyword": "ISTJ", "style": "magazine", "nickname": "해일", "signature": "💜 응원합니다\n\n🌿 행복하세요"}
    doc = writer.write(f, inp, "ISTJ 특징", [{"heading": "🔍 특징"}])
    pr = f.prompts[0]
    assert "감성 정리형" in pr and "🧂 “한번 맡은 일은 끝까지”" in pr and "블로거 닉네임: 해일" in pr
    assert "해일의 한마디" in pr and "outro_heading" in pr and '"### "' in pr
    assert doc["signature"] == ["💜 응원합니다", "🌿 행복하세요"]
    basic = FakeLLM()
    writer.write(basic, {"keyword": "a"}, "t", [])
    assert "감성 정리형" not in basic.prompts[0] and "outro_heading" not in basic.prompts[0]


def test_custom_style_requires_example():
    with pytest.raises(ValueError, match="예시 글"):
        writer.normalize({"keyword": "a", "style": "custom", "style_example": "짧음"})
    f = FakeLLM()
    ex = "나만의 문체 예시입니다. " * 10
    writer.plan(f, {"keyword": "a", "style": "custom", "style_example": ex})
    assert ex.strip()[:30] in f.prompts[0] and "내 글 스타일" in f.prompts[0]


def test_magazine_rendering():
    d = writer.clean_doc({"title": "t", "intro": "🧂 “한번 맡은 일은 끝까지” 소금형\n\n문단.\n\n“대사입니다.”\n\n라고 했다.\n“이건 문단 중간 인용”",
                          "sections": [{"heading": "🔍 특징", "content": "### 📌 세부\n\n설명\n\n📋 책임감\n\n→ 떠맡을 수 있습니다."}],
                          "outro": "끝 🌿", "outro_heading": "🌿 해일의 한마디", "signature": ["💜 응원"], "hashtags": ["a"]})
    h = writer.to_html(d)
    assert 'font-size:19px;font-weight:bold;">🧂' in h                          # 부제
    assert 'color:#333;">“대사입니다.”' in h                                   # 단독 대사 강조
    assert "라고 했다.<br>“이건 문단 중간 인용”" in h                            # 문단 속 따옴표는 그대로
    assert 'font-size:17px;font-weight:bold;line-height:1.7;margin:22px 0 10px;">📌 세부' in h
    assert 'padding-left:14px;color:#555;">→ 떠맡을' in h
    assert h.index("🌿 해일의 한마디") < h.index("끝 🌿") < h.index("💜 응원") < h.index("#a")
    t = writer.to_text(d)
    assert "###" not in t and "📌 세부" in t and t.rstrip().endswith("#a") and "💜 응원" in t


# ------------------------------------------------------------ 이미지 생성기
def test_gemini_image_discovers_model_and_decodes(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            self._send(200, {"models": [{"name": "models/gemini-x-flash", "supportedGenerationMethods": ["generateContent"]},
                                        {"name": "models/gemini-x-flash-image", "supportedGenerationMethods": ["generateContent"]},
                                        {"name": "models/imagen-4", "supportedGenerationMethods": ["predict"]}]})

        def do_POST(self):
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._send(200, {"candidates": [{"content": {"parts": [{"text": "여기"}, {"inlineData": {
                "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]}}]})

    srv, base = serve(H)
    g = images.GeminiImage("K", base=base + "/v1beta")
    assert g.find_models() == ["gemini-x-flash-image"]
    data, mime = g.generate("고양이", "1:1")
    assert data == PNG and mime == "image/png" and seen["path"].endswith("/gemini-x-flash-image:generateContent")
    assert seen["body"]["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    srv.shutdown()


def test_gemini_image_quota_message(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(429); self.end_headers(); self.wfile.write(b'{"error":{"message":"quota"}}')

    srv, base = serve(H)
    with pytest.raises(images.ImageError) as e:
        images.GeminiImage("K", "gemini-img", base=base + "/v1beta").generate("p")
    assert e.value.status == 429 and "Pollinations" in str(e.value)
    with pytest.raises(images.ImageError):
        images.GeminiImage("")
    srv.shutdown()


def test_pollinations_client():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            seen["path"] = self.path
            body = PNG * 200 if "ok" in self.path else b"<html>busy</html>"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg" if "ok" in self.path else "text/html")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    srv, base = serve(H)
    data, mime = images.Pollinations(base + "/prompt/").generate("ok 고양이", "16:9")
    assert mime == "image/jpeg" and len(data) > 1000 and "width=1344" in seen["path"] and "nologo=true" in seen["path"]
    with pytest.raises(images.ImageError):
        images.Pollinations(base + "/prompt/").generate("bad", "1:1")
    srv.shutdown()


def test_prompt_builder():
    p = images.build_prompt("A laptop on a desk", "character", "16:9")
    assert p.startswith("A laptop on a desk.") and "character" in p and "No text" in p
    with pytest.raises(ValueError):
        images.build_prompt("  ", "illust", "1:1")


def test_image_storage_safety(tmp_path):
    n = images.save(tmp_path, 3, PNG, "image/png")
    assert images.list_images(tmp_path, 3) == [n] and images.path_of(tmp_path, "3", n)
    for bad in [("3", "../x.png"), ("3", "settings.json"), ("../3", n), ("x", n), ("3", "img-1.png")]:
        assert images.path_of(tmp_path, *bad) is None
    images.delete(tmp_path, 3, "../../evil")  # 무시
    images.delete(tmp_path, 3, n)
    assert images.list_images(tmp_path, 3) == []


# ------------------------------------------------------------ 웹 API
class FakeImg:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, aspect="1:1"):
        self.prompts.append((prompt, aspect))
        return PNG, "image/png"


def test_web_images_and_style_settings(tmp_path, monkeypatch):
    for k in ("GEMINI_API_KEY", "BLOG_NICKNAME", "BLOG_SIGNATURE", "STYLE_EXAMPLE", "IMAGE_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(tmp_path / "s.json")
    s.update({"gemini_api_key": "g", "nickname": "해일", "signature": "💜 응원합니다"})
    fake_llm, fake_img = FakeLLM(), FakeImg()
    app = App(s, Store(":memory:"), llm_factory=lambda st: fake_llm, image_factory=lambda st: fake_img,
              images_dir=tmp_path / "images")
    r = app.handle("POST", "/api/writer/write", {}, {"input": {"keyword": "ISTJ", "style": "magazine"}, "title": "t",
                                                       "outline": [], "use_refs": False})
    assert "해일" in fake_llm.prompts[-1] and r["doc"]["signature"] == ["💜 응원합니다"] and "💜 응원합니다" in r["html"]
    assert app.handle("GET", "/api/writer/options", {}, {})["image"]["aspects"]

    with pytest.raises(ApiError):
        app.handle("POST", "/api/images", {}, {"doc_id": 999, "desc": "x"})
    with pytest.raises(ApiError):
        app.handle("POST", "/api/images", {}, {"doc_id": r["id"], "desc": " "})
    img = app.handle("POST", "/api/images", {}, {"doc_id": r["id"], "desc": "고양이", "style": "watercolor", "aspect": "4:3"})
    assert img["url"].startswith(f"/img/{r['id']}/") and "watercolor" in fake_img.prompts[0][0] and fake_img.prompts[0][1] == "4:3"
    assert [x["name"] for x in app.handle("GET", "/api/images", {"doc_id": str(r["id"])}, {})] == [img["name"]]

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    with urllib.request.urlopen(base + img["url"]) as resp:
        assert resp.read() == PNG and resp.headers["Content-Type"] == "image/png"
    for bad in (f"/img/{r['id']}/..%2Fs.json", f"/img/{r['id']}/x/y", "/img/../s.json"):
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + bad)
    srv.shutdown()

    app.handle("DELETE", f"/api/docs/{r['id']}", {}, {})
    assert not (tmp_path / "images" / str(r["id"])).exists()

    # 공개 설정은 빈 값으로 지울 수 있고, 비밀 키는 빈 값이면 유지
    app.handle("POST", "/api/settings", {}, {"nickname": "", "gemini_api_key": ""})
    assert app.settings.get("nickname") == "" and app.settings.get("gemini_api_key") == "g"


def test_normalize_is_idempotent():
    once = writer.normalize({"keyword": "a", "signature": "x\n\ny", "style": "magazine"})
    assert writer.normalize(once) == once and once["signature"] == ["x", "y"]


def test_to_english_prompt_uses_context_and_falls_back():
    class L:
        def __init__(self, out): self.out, self.user = out, ""
        def complete(self, system, user, json_mode=True, temperature=0.8):
            self.user = user
            if isinstance(self.out, Exception):
                raise self.out
            return self.out
    l = L('{"prompt": "A tidy wooden desk with a laptop and a planner, morning light"}')
    assert images.to_english(l, "책상 사진", "ISTJ 특징 총정리", "ISTJ").startswith("A tidy wooden desk")
    assert "ISTJ 특징 총정리" in l.user and "책상 사진" in l.user
    assert images.to_english(L(llm.LLMError("busy", 503)), "책상", "t", "k") == ""
    assert images.to_english(L("not json"), "책상", "t", "k") == ""


def test_web_image_uses_english_prompt(tmp_path, monkeypatch):
    for k in ("GEMINI_API_KEY", "IMAGE_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(tmp_path / "s.json"); s.update({"gemini_api_key": "g"})

    class L(FakeLLM):
        def complete(self, system, user, json_mode=True, temperature=0.8):
            if "image generator" in system:
                self.prompts.append(user)
                return '{"prompt": "A calm office worker at a desk"}'
            return super().complete(system, user, json_mode, temperature)

    fl, fi = L(), FakeImg()
    app = App(s, Store(":memory:"), llm_factory=lambda st: fl, image_factory=lambda st: fi, images_dir=tmp_path / "im")
    r = app.handle("POST", "/api/writer/write", {}, {"input": {"keyword": "ISTJ"}, "title": "ISTJ 정리", "outline": [], "use_refs": False})
    img = app.handle("POST", "/api/images", {}, {"doc_id": r["id"], "desc": "책상에서 일하는 사람", "style": "illust"})
    assert img["prompt_en"] == "A calm office worker at a desk" and fi.prompts[-1][0].startswith("A calm office worker at a desk.")
    assert "캠핑 준비물 체크리스트 총정리" in fl.prompts[-1] or "ISTJ" in fl.prompts[-1]
    # 직접 쓴 영어 설명이 있으면 AI 변환 없이 그대로
    n = len(fl.prompts)
    img = app.handle("POST", "/api/images", {}, {"doc_id": r["id"], "desc": "x", "prompt_en": "A red bicycle"})
    assert fi.prompts[-1][0].startswith("A red bicycle.") and len(fl.prompts) == n and img["prompt_en"] == "A red bicycle"
