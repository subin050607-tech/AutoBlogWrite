import base64
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from autoblog import workflow
from autoblog.config import DEFAULTS, _merge
from autoblog.db import DB
from autoblog.generator import MockGenerator
from autoblog.publishers import FilePublisher, PublishResult
from autoblog.web import App, ApiError, make_handler
from http.server import ThreadingHTTPServer


def mk(tmp):
    cfg = _merge(DEFAULTS, {"llm": {"provider": "mock"}, "blog": {"publisher": "file"},
                            "persona": {"min_chars": 200},
                            "paths": {"db": ":memory:", "output": str(tmp / "out"), "images": str(tmp / "img")}})
    db = DB(":memory:")
    return cfg, db, MockGenerator(cfg), FilePublisher(cfg)


def test_full_flow_draft_review_schedule_publish(tmp_path):
    cfg, db, gen, pub = mk(tmp_path)
    tid = db.add_topic("제주도 여행")
    aid = workflow.create_draft(db, cfg, gen, tid, "제주도 여행 정리", ["준비물", "코스", "마무리"])
    a = db.get_article(aid)
    assert a["status"] == "reviewed" and a["title"] == "제주도 여행 정리"
    assert [h for h in ("준비물", "코스", "마무리") if f"## {h}" in a["body_md"]] == ["준비물", "코스", "마무리"]
    assert db.get_topic(tid)["status"] == "drafted"
    assert db.add_topic("제주도 여행") is None  # 작성 중 키워드도 중복 차단

    workflow.schedule(db, aid, datetime.now() - timedelta(minutes=1))
    assert workflow.publish_due(db, cfg, lambda: pub) == 1
    assert db.get_article(aid)["status"] == "published"
    assert db.get_topic(tid)["status"] == "done" and len(db.list_posts()) == 1
    with pytest.raises(workflow.WorkflowError):
        workflow.save_edit(db, cfg, aid, title="x")


def test_unreviewed_or_issue_articles_cannot_publish(tmp_path):
    cfg, db, gen, pub = mk(tmp_path)
    aid = db.add_article(None, "짧은 글", "본문", ["a"])
    workflow.review(db, cfg, aid)
    a = db.get_article(aid)
    assert a["status"] == "draft" and a["issues"]
    with pytest.raises(workflow.WorkflowError):
        workflow.schedule(db, aid, None)
    with pytest.raises(workflow.WorkflowError):
        workflow.publish(db, cfg, pub, aid)


def test_edit_resets_schedule_and_missing_image_flagged(tmp_path):
    cfg, db, gen, pub = mk(tmp_path)
    tid = db.add_topic("캠핑")
    aid = workflow.create_draft(db, cfg, gen, tid)
    workflow.schedule(db, aid, datetime.now() + timedelta(days=1))
    body = db.get_article(aid)["body_md"] + "\n\n![x](nope.png)"
    issues = workflow.save_edit(db, cfg, aid, body_md=body)
    a = db.get_article(aid)
    assert a["status"] == "draft" and a["scheduled_at"] is None
    assert any("nope.png" in i for i in issues)


def test_failed_publish_retries_then_gives_up(tmp_path):
    cfg, db, gen, _ = mk(tmp_path)
    cfg["schedule"]["max_retries"] = 1

    class Boom:
        def publish(self, *a, **k):
            raise RuntimeError("네이버 오류")

        def upload_image(self, p):
            return None

    tid = db.add_topic("실패 테스트")
    aid = workflow.create_draft(db, cfg, gen, tid)
    t0 = datetime.now()
    workflow.schedule(db, aid, t0 - timedelta(minutes=1))
    workflow.publish_due(db, cfg, lambda: Boom(), t0)
    a = db.get_article(aid)
    assert a["status"] == "scheduled" and a["retries"] == 1 and a["scheduled_at"] > t0.isoformat()
    workflow.publish_due(db, cfg, lambda: Boom(), t0 + timedelta(minutes=11))
    a = db.get_article(aid)
    assert a["status"] == "failed" and "네이버 오류" in a["last_error"]


def test_dashboard_counts(tmp_path):
    cfg, db, gen, pub = mk(tmp_path)
    tid = db.add_topic("대시보드")
    aid = workflow.create_draft(db, cfg, gen, tid)
    workflow.publish(db, cfg, pub, aid)
    d = db.dashboard()
    assert d["articles_total"] == 1 and d["articles_by_status"]["published"] == 1
    assert d["published_today"] == 1 and d["published_month"] == 1


@pytest.fixture
def server(tmp_path):
    cfg, db, gen, pub = mk(tmp_path)
    app = App(cfg, db, gen, lambda: pub)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, 0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", app
    srv.shutdown()


def call(base, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, method=method, headers=h,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_end_to_end_and_security(server):
    base, app = server
    assert call(base, "GET", "/")[0] == 200
    s, b = call(base, "POST", "/api/topics", {"keyword": "웹 테스트", "notes": "메모"})
    tid = json.loads(b)["id"]
    assert call(base, "POST", "/api/topics", {"keyword": "웹 테스트"})[0] == 409
    s, b = call(base, "POST", f"/api/topics/{tid}/draft", {"title": "웹 테스트 제목", "outline": ["가", "나"]})
    aid = json.loads(b)["article_id"]
    s, b = call(base, "GET", f"/api/articles/{aid}")
    assert json.loads(b)["status"] == "reviewed"
    assert "웹 테스트 제목" in call(base, "GET", f"/api/articles/{aid}/preview")[1]
    assert call(base, "POST", f"/api/articles/{aid}/publish", {})[0] == 200
    assert call(base, "POST", f"/api/articles/{aid}/publish", {})[0] == 409
    assert "웹 테스트 제목" in call(base, "GET", "/api/export/posts.csv")[1]

    # 보안: 잘못된 Host / form content-type 차단
    assert call(base, "GET", "/api/dashboard", headers={"Host": "evil.example.com"})[0] == 403
    assert call(base, "POST", "/api/topics", {"keyword": "x"}, headers={"Content-Type": "text/plain"})[0] == 415


def test_image_upload_rules(server):
    base, app = server
    tid = json.loads(call(base, "POST", "/api/topics", {"keyword": "이미지"})[1])["id"]
    png = base64.b64encode(b"\x89PNG fake").decode()
    assert call(base, "POST", f"/api/topics/{tid}/upload", {"filename": "a.png", "data": png})[0] == 200
    assert call(base, "POST", f"/api/topics/{tid}/upload", {"filename": "../evil.png", "data": png})[0] == 200  # basename 으로 무력화
    assert not (app._images_dir(tid).parent.parent / "evil.png").exists()
    assert call(base, "POST", f"/api/topics/{tid}/upload", {"filename": "a.exe", "data": png})[0] == 400
    assert call(base, "POST", f"/api/topics/{tid}/upload", {"filename": "b.png", "data": "!!"})[0] == 400
    assert sorted(json.loads(call(base, "POST", f"/api/topics/{tid}/images", {})[1])["images"]) == ["a.png", "evil.png"]
    assert call(base, "GET", f"/img/{tid}/a.png")[0] == 200
    assert call(base, "GET", f"/img/{tid}/..%2F..%2Fconfig.yaml")[0] == 404
