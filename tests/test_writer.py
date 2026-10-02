import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from blogscope import llm, writer
from blogscope.settings import Settings
from blogscope.store import Store
from blogscope.web import App, ApiError
from fakes import FakeAPI, FakeAd, FakeLLM

INP = {"keyword": "캠핑 준비물", "purpose": "list", "tone": "pro", "pattern": "hospital", "facts": "주차 가능",
       "faq": True, "photo_marks": True}


def test_normalize_defaults_and_validation():
    n = writer.normalize({"keyword": " 캠핑 ", "purpose": "evil", "tone": None, "pattern": "x"})
    assert n["keyword"] == "캠핑" and n["purpose"] == "info" and n["tone"] == "friendly" and n["pattern"] == "general"
    assert n["photo_marks"] is True and n["faq"] is False
    with pytest.raises(ValueError):
        writer.normalize({"keyword": " "})


def test_plan_parses_and_prompt_has_rules_and_refs():
    f = FakeLLM()
    p = writer.plan(f, INP, [{"title": "상위글", "description": "요약"}])
    assert len(p["titles"]) == 5 and [o["heading"] for o in p["outline"]] == ["텐트 고르기", "의자와 테이블"]
    assert p["hashtags"] == ["캠핑", "캠핑준비물"]
    pr = f.prompts[0]
    assert "병·의원" in pr and "최상급" in pr and "주차 가능" in pr and "상위글" in pr and "추천·리스트형" in pr


def test_write_rewrite_and_render():
    f = FakeLLM()
    doc = writer.write(f, INP, "제목A", [{"heading": "텐트 고르기", "points": ["크기"]}])
    assert len(doc["sections"]) == 5 and doc["faq"] and "1. 텐트 고르기 — 크기" in f.prompts[0]
    assert writer.count_photo_marks(doc) == 5
    h = writer.to_html(doc)
    assert "<h3" in h and "<b>핵심</b>" in h and "📷" in h and "<ul" in h and "#캠핑준비물" in h and "Q. 텐트" in h
    t = writer.to_text(doc)
    assert "**" not in t and "텐트 고르기" in t and t.endswith("#캠핑 #캠핑준비물")
    new = writer.rewrite_part(f, INP, doc, "2", "더 짧게")
    assert new == {"heading": "새 소제목", "content": "다시 쓴 내용입니다.\n\n- 항목"} and "더 짧게" in f.prompts[-1]
    assert writer.rewrite_part(f, INP, doc, "intro")["content"]
    for bad in ("9", "-1", "x"):
        with pytest.raises(ValueError):
            writer.rewrite_part(f, INP, doc, bad)


def test_html_escapes_user_content():
    doc = writer.clean_doc({"title": "t", "intro": "<script>alert(1)</script> **굵게**", "sections": [{"heading": "<img src=x>", "content": "a"}],
                            "hashtags": ["<b>"]})
    h = writer.to_html(doc)
    assert "<script>" not in h and "&lt;script&gt;" in h and "<img" not in h and "<b>굵게</b>" in h
    with pytest.raises(ValueError):
        writer.clean_doc({"sections": "x"})


def test_faq_omitted_when_not_requested():
    doc = writer.write(FakeLLM(), {**INP, "faq": False}, "t", [])
    assert doc["faq"] == []


def test_parse_json_variants():
    assert llm.parse_json('잡음 {"a": 1} 끝') == {"a": 1}
    assert llm.parse_json("```json\n[1,2]\n```") == [1, 2]
    with pytest.raises(llm.LLMError):
        llm.parse_json("없음")


def test_gemini_client_against_fake_server(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    seen = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            seen["n"] += 1
            seen["path"], seen["key"] = self.path, self.headers.get("x-goog-api-key")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if seen["n"] == 1:  # 첫 요청은 한도 초과 → 재시도 확인
                self.send_response(429); self.end_headers(); self.wfile.write(b"{}"); return
            out = {"candidates": [{"content": {"parts": [{"text": "생각중", "thought": True}, {"text": '{"ok": true}'}]}}]}
            if "block" in json.dumps(seen["body"]):
                out = {"promptFeedback": {"blockReason": "SAFETY"}}
            b = json.dumps(out).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    g = llm.Gemini("KEY", "gemini-test", base=f"http://127.0.0.1:{srv.server_port}/v1beta")
    assert llm.complete_json(g, "sys", "user") == {"ok": True}
    assert seen["n"] == 2 and seen["path"] == "/v1beta/models/gemini-test:generateContent" and seen["key"] == "KEY"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    with pytest.raises(llm.LLMError, match="SAFETY"):
        g.complete("s", "block")
    with pytest.raises(llm.LLMError):
        llm.Gemini("")
    srv.shutdown()


def make_app(tmp_path, with_llm=True):
    s = Settings(tmp_path / "s.json")
    s.update({"naver_client_id": "c", "naver_client_secret": "s", "searchad_api_key": "a", "searchad_secret": "b",
              "searchad_customer_id": "1", **({"gemini_api_key": "g"} if with_llm else {})})
    fake = FakeLLM()
    return App(s, Store(":memory:"), api_factory=lambda st: FakeAPI(), ad_factory=lambda st: FakeAd(),
               llm_factory=lambda st: fake), fake


def test_writer_api_flow(tmp_path, monkeypatch):
    for k in ("GEMINI_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    app, fake = make_app(tmp_path)
    opts = app.handle("GET", "/api/writer/options", {}, {})
    assert "hospital" in opts["patterns"] and opts["pattern_rules"]["hospital"]
    plan = app.handle("POST", "/api/writer/plan", {}, {"input": {"keyword": "캠핑 준비물"}})
    assert plan["refs"] and plan["volume"]["total"] == 10000 and "남의 글" in fake.prompts[-1]
    r = app.handle("POST", "/api/writer/write", {}, {"input": plan["input"], "title": plan["titles"][0],
                                                       "outline": plan["outline"] + [{"bad": 1}, "x"], "titles": plan["titles"]})
    assert r["id"] and r["diagnosis"]["score"] > 0 and r["diagnosis"]["stats"]["images"] == 5 and "<h3" in r["html"]
    # 화면에서 고친 내용 저장
    doc = r["doc"]; doc["title"] = "고친 제목"
    r2 = app.handle("POST", f"/api/docs/{r['id']}", {}, {"doc": doc})
    assert r2["id"] == r["id"] and r2["doc"]["title"] == "고친 제목"
    # 섹션 다시 쓰기
    r3 = app.handle("POST", "/api/writer/rewrite", {}, {"id": r["id"], "part": "1", "doc": r2["doc"]})
    assert r3["doc"]["sections"][1]["heading"] == "새 소제목" and r3["doc"]["title"] == "고친 제목"
    lst = app.handle("GET", "/api/docs", {}, {})
    assert len(lst) == 1 and lst[0]["title"] == "고친 제목"
    got = app.handle("GET", f"/api/docs/{r['id']}", {}, {})
    assert got["doc"]["sections"][1]["heading"] == "새 소제목"
    assert app.handle("GET", "/api/docs", {}, {})[0]["updated_at"] == lst[0]["updated_at"]  # GET 은 저장 안 함
    app.handle("DELETE", f"/api/docs/{r['id']}", {}, {})
    with pytest.raises(ApiError):
        app.handle("GET", f"/api/docs/{r['id']}", {}, {})


def test_writer_requires_llm_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app, _ = make_app(tmp_path, with_llm=False)
    with pytest.raises(ApiError) as e:
        app.handle("POST", "/api/writer/plan", {}, {"input": {"keyword": "a"}})
    assert e.value.status == 412 and "Gemini" in str(e.value)
    with pytest.raises(ApiError) as e:
        app.handle("POST", "/api/writer/plan", {}, {"input": {"keyword": ""}})
    assert e.value.status == 400


def test_llm_errors_become_api_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app, fake = make_app(tmp_path)

    def boom(*a, **k):
        raise llm.LLMError("무료 사용 한도를 넘었습니다", 429)
    fake.complete = boom
    with pytest.raises(ApiError) as e:
        app.handle("POST", "/api/writer/plan", {}, {"input": {"keyword": "a"}})
    assert e.value.status == 429 and "한도" in str(e.value)


def test_gemini_switches_to_suggested_model_on_404(tmp_path, monkeypatch):
    paths = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            paths.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            if "old-model" in self.path:
                b = json.dumps({"error": {"code": 404, "message": "This model models/old-model is no longer available to new users. "
                                          "Please update your code to use models/new-model-9 for the latest features.",
                                          "status": "NOT_FOUND"}}).encode()
                self.send_response(404)
            else:
                b = json.dumps({"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]}).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/v1beta"
    g = llm.Gemini("K", "old-model", base=base)
    assert llm.parse_json(g.complete("s", "u")) == {"ok": 1}
    assert g.model == g.switched_to == "new-model-9" and paths[-1].endswith("/models/new-model-9:generateContent")

    # 안내가 없는 404 는 그대로 오류
    class H2(H):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(404); self.end_headers(); self.wfile.write(b'{"error":"nope"}')
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), H2)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    with pytest.raises(llm.LLMError) as e:
        llm.Gemini("K", "x", base=f"http://127.0.0.1:{srv2.server_port}/v1beta").complete("s", "u")
    assert e.value.status == 404

    # 웹 앱: 전환된 모델이 설정에 저장된다
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app, _ = make_app(tmp_path)
    app._llm_factory = lambda st: llm.Gemini("K", st.get("llm_model") or "old-model", base=base)
    monkeypatch.setattr(writer, "plan", lambda c, i, r: (c.complete("s", "u"), {"titles": ["t"], "outline": [], "hashtags": []})[1])
    app.handle("POST", "/api/writer/plan", {}, {"input": {"keyword": "k"}, "use_refs": False})
    assert app.settings.get("llm_model") == "new-model-9"
    srv.shutdown(); srv2.shutdown()


def test_gemini_busy_retries_then_friendly_error(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    n = {"c": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            n["c"] += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503); self.end_headers()
            self.wfile.write(b'{"error":{"code":503,"message":"This model is currently experiencing high demand."}}')

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with pytest.raises(llm.LLMError) as e:
        llm.Gemini("K", "m", base=f"http://127.0.0.1:{srv.server_port}/v1beta").complete("s", "u")
    assert n["c"] == 5 and e.value.status == 503 and "혼잡" in str(e.value) and "high demand" not in str(e.value)
    srv.shutdown()
