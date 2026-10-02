import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from blogscope import naver
from blogscope.settings import Settings
from blogscope.store import Store
from blogscope.web import App, make_handler
from fakes import POST_HTML, FakeAd, FakeAPI, rss_xml


def make_app(tmp_path, keys=True):
    s = Settings(tmp_path / "settings.json")
    if keys:
        s.update({"naver_client_id": "cid", "naver_client_secret": "csecret123", "searchad_api_key": "akey123",
                  "searchad_secret": "asecret", "searchad_customer_id": "999"})
    calls = {"rss": 0}

    def rss(bid):
        calls["rss"] += 1
        if bid == "nobody":
            raise naver.NaverError("RSS를 읽을 수 없습니다.", 404)
        return naver.parse_rss(rss_xml(bid, n=15), bid)

    app = App(s, Store(":memory:"), rss_fetcher=rss, post_fetcher=lambda b, n: naver.parse_post_html(POST_HTML),
              api_factory=lambda st: FakeAPI(), ad_factory=lambda st: FakeAd())
    return app, calls


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    app, calls = make_app(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", app, calls
    srv.shutdown()


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null") if "json" in r.headers["Content-Type"] else r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_blog_endpoint_caches_and_records(server):
    base, app, calls = server
    s, r = call(base, "POST", "/api/blog", {"blog": "https://blog.naver.com/tester"})
    assert s == 200 and r["blog"]["blog_id"] == "tester" and r["exposure_measured"] and len(r["history"]) == 1
    s, r2 = call(base, "POST", "/api/blog", {"blog": "tester"})
    assert r2["cached"] and calls["rss"] == 1
    s, r3 = call(base, "POST", "/api/blog", {"blog": "tester", "refresh": True})
    assert calls["rss"] == 2 and "cached" not in r3 and len(r3["history"]) == 2
    s, hist = call(base, "GET", "/api/history?kind=blog")
    assert hist[0]["key"] == "tester" and hist[0]["n"] == 2
    s, d = call(base, "GET", "/api/history?kind=blog&key=tester")
    assert len(d["history"]) == 2 and d["latest"]["blog"]["blog_id"] == "tester"
    assert call(base, "DELETE", "/api/history?kind=blog&key=tester")[0] == 200
    assert call(base, "GET", "/api/history?kind=blog")[1] == []


def test_errors_are_friendly(server):
    base, *_ = server
    s, r = call(base, "POST", "/api/blog", {"blog": "https://evil.com/x"})
    assert s == 400 and "네이버 블로그" in r["error"]
    s, r = call(base, "POST", "/api/blog", {"blog": "nobody"})
    assert s == 400 and "RSS" in r["error"]
    s, r = call(base, "POST", "/api/rank", {"blog": "tester", "keywords": " \n "})
    assert s == 400
    s, r = call(base, "POST", "/api/compare", {"blogs": ["tester"]})
    assert s == 400
    assert call(base, "GET", "/api/nothing")[0] == 404


def test_keyword_rank_post_compare(server):
    base, *_ = server
    s, r = call(base, "POST", "/api/keyword", {"keyword": "캠핑 준비물"})
    assert s == 200 and r["volume"]["total"] == 10000 and r["related"]
    s, r = call(base, "POST", "/api/rank", {"blog": "tester", "keywords": "캠핑,텐트"})
    assert [x["rank"] for x in r["results"]] == [2, 2]
    s, r = call(base, "POST", "/api/post", {"url": "https://blog.naver.com/tester/224410368545", "keyword": "캠핑 준비물"})
    assert s == 200 and r["source"] == "url" and r["stats"]["images"] == 2 and r["title"] == "캠핑 준비물 총정리"
    s, r = call(base, "POST", "/api/post", {"url": "https://blog.naver.com/tester"})
    assert s == 400 and "글 번호" in r["error"]
    s, r = call(base, "POST", "/api/post", {"title": "제목", "text": "본문 내용입니다."})
    assert s == 200 and r["source"] == "paste"
    s, r = call(base, "POST", "/api/compare", {"blogs": ["tester", "other", "nobody", ""]})
    assert s == 200 and len(r["blogs"]) == 3 and "error" in r["blogs"][2] and r["blogs"][0]["score"] > 0


def test_settings_masking_and_missing_keys(tmp_path, monkeypatch):
    for k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    app, _ = make_app(tmp_path, keys=False)
    pub = app.handle("GET", "/api/settings", {}, {})
    assert not pub["has_search"] and not pub["has_searchad"]
    from blogscope.web import ApiError
    with pytest.raises(ApiError) as e:
        app.handle("POST", "/api/rank", {}, {"blog": "tester", "keywords": ["a"]})
    assert e.value.status == 412
    with pytest.raises(ApiError):
        app.handle("POST", "/api/keyword", {}, {"keyword": "a"})
    rep = app.handle("POST", "/api/blog", {}, {"blog": "tester"})  # 키 없이도 블로그 분석은 동작
    assert not rep["exposure_measured"]

    pub = app.handle("POST", "/api/settings", {}, {"naver_client_id": "id1", "naver_client_secret": "supersecret",
                                                   "unknown": "x"})
    assert pub["has_search"] and pub["naver_client_secret"].startswith("supe") and "secret" not in pub["naver_client_secret"]
    assert "unknown" not in json.loads((tmp_path / "settings.json").read_text())
    app.handle("POST", "/api/settings", {}, {"naver_client_secret": ""})  # 빈 값은 기존 값 유지
    assert app.settings.get("naver_client_secret") == "supersecret"
    monkeypatch.setenv("NAVER_CLIENT_ID", "from-env")
    assert app.settings.get("naver_client_id") == "from-env"


def test_http_security(server):
    base, *_ = server
    assert call(base, "GET", "/api/settings", headers={"Host": "evil.example.com"})[0] == 403
    assert call(base, "POST", "/api/blog", {"blog": "tester"}, headers={"Content-Type": "text/plain"})[0] == 415
    req = urllib.request.Request(base + "/api/blog", method="POST", headers={"Content-Type": "application/json"}, data=b"[1]")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400
    with urllib.request.urlopen(base + "/") as r:
        assert "BlogScope" in r.read().decode() and "unsafe-inline" in r.headers["Content-Security-Policy"]
    s, _ = call(base, "GET", "/api/settings")
    with urllib.request.urlopen(base + "/api/settings") as r:
        assert "script-src 'none'" in r.headers["Content-Security-Policy"]


def test_openapi_hub_and_legacy_requests(monkeypatch):
    seen = []

    def fake_request(url, headers=None, data=None, method=None, timeout=15):
        seen.append((url, headers, data))
        if "trend" in url or "datalab" in url:
            return json.dumps({"results": [{"data": [{"period": "2026-01-01", "ratio": 50}]}]}).encode()
        return json.dumps({"total": 3, "items": [{"title": "<b>캠핑</b>", "link": "https://blog.naver.com/a/1",
                                                  "bloggername": "a", "bloggerlink": "", "postdate": "20260101"}]}).encode()

    monkeypatch.setattr(naver, "request", fake_request)
    hub = naver.OpenAPI("id", "sec")
    r = hub.search_blog("캠핑", display=10)
    assert r["items"][0]["title"] == "캠핑" and r["total"] == 3
    url, h, _ = seen[-1]
    assert url.startswith("https://naverapihub.apigw.ntruss.com/search/v1/blog?") and "query=%EC%BA%A0%ED%95%91" in url
    assert h == {"X-NCP-APIGW-API-KEY-ID": "id", "X-NCP-APIGW-API-KEY": "sec"}
    hub.trend("캠핑")
    assert seen[-1][0] == "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
    assert json.loads(seen[-1][2])["keywordGroups"][0]["keywords"] == ["캠핑"]

    old = naver.OpenAPI("id", "sec", hub=False)
    old.search_blog("x")
    assert seen[-1][0].startswith("https://openapi.naver.com/v1/search/blog.json?")
    assert seen[-1][1] == {"X-Naver-Client-Id": "id", "X-Naver-Client-Secret": "sec"}
    old.trend("x")
    assert seen[-1][0] == "https://openapi.naver.com/v1/datalab/search"


def test_settings_api_source(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVER_API_SOURCE", raising=False)
    s = Settings(tmp_path / "s.json")
    assert s.public()["naver_api_source"] == "hub"
    s.update({"naver_api_source": "developers"})
    assert s.public()["naver_api_source"] == "developers"
