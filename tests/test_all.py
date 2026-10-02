import random
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from autoblog.config import DEFAULTS, _merge
from autoblog.db import DB
from autoblog.generator import Article, MockGenerator, parse_article
from autoblog.pipeline import QualityError, process_topic
from autoblog.publishers import FilePublisher, NaverPublisher
from autoblog.quality import check_article
from autoblog.render import render_html
from autoblog.scheduler import Scheduler


def cfg(tmp, **over):
    c = _merge(DEFAULTS, {"llm": {"provider": "mock"}, "blog": {"publisher": "file"},
                          "paths": {"db": ":memory:", "output": str(tmp / "out"), "images": str(tmp / "img")}})
    return _merge(c, over)


def test_parse_article_handles_fences():
    a = parse_article('```json\n{"title":"T","body_md":"b","tags":["#a","b"]}\n```')
    assert a.title == "T" and a.tags == ["a", "b"]
    with pytest.raises(ValueError):
        parse_article("no json here")


def test_render_escapes_and_formats():
    h = render_html("## 제목\n\n<script>x</script> **굵게**\n\n- a\n- b\n\n1. x\n2. y", tags=["a b"], disclosure="D")
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert "<strong>굵게</strong>" in h and "<ul" in h and "<ol" in h and "#ab" in h and ">D<" in h


def test_render_image_resolution_and_missing():
    h = render_html("![설명](a.png)\n\n![x](missing.png)", resolve_image=lambda s: "http://u/" + s if s == "a.png" else None)
    assert 'src="http://u/a.png"' in h and "missing" not in h


def test_quality_checks(tmp_path):
    c = cfg(tmp_path)
    good = MockGenerator(c).generate({"keyword": "제주 여행", "meta": {}})
    assert check_article(good, "제주 여행", c, []) == []
    short = Article("제주 여행", "짧음", ["a"])
    assert any("짧" in i for i in check_article(short, "제주 여행", c, []))
    assert any("비슷" in i for i in check_article(good, "제주 여행", c, [good.title]))
    c["quality"]["banned_words"] = ["차근차근"]
    assert any("금지어" in i for i in check_article(good, "제주 여행", c, []))
    assert any("제목" in i for i in check_article(Article("딴 제목", good.body_md, ["a"]), "제주 여행", c, []))


def test_pipeline_file_publish_and_dedup(tmp_path):
    c = cfg(tmp_path); db = DB(":memory:")
    assert db.add_topic("제주 여행") and db.add_topic("제주 여행") is None
    t = db.next_topic()
    res = process_topic(t, c, db, MockGenerator(c), FilePublisher(c))
    assert Path(res.file_path).exists() and db.get_topic(t["id"])["status"] == "done"
    assert len(db.list_posts()) == 1


def test_pipeline_quality_failure_marks_failed(tmp_path):
    c = cfg(tmp_path, quality={"max_regenerations": 1}); db = DB(":memory:")
    db.add_topic("제주 여행"); t = db.next_topic()

    class Bad:
        calls = 0
        def generate(self, topic, feedback=None):
            Bad.calls += 1
            return Article("무관", "짧", [])
    with pytest.raises(QualityError):
        process_topic(t, c, db, Bad(), FilePublisher(c))
    assert Bad.calls == 2 and db.get_topic(t["id"])["status"] == "failed"


def test_preview_does_not_record(tmp_path):
    c = cfg(tmp_path); db = DB(":memory:"); db.add_topic("k"); t = db.next_topic()
    process_topic(t, c, db, MockGenerator(c), FilePublisher(c), record=False)
    assert db.get_topic(t["id"])["status"] == "pending" and db.list_posts() == []


class FakeMW:
    def __init__(self): self.posts, self.media = [], []
    def newPost(self, blog, user, pw, post, pub): self.posts.append((post, pub)); return 123
    def newMediaObject(self, blog, user, pw, d): self.media.append(d["name"]); return {"url": "https://img/" + d["name"]}
    def getRecentPosts(self, *a): return []

class FakeServer:
    def __init__(self): self.metaWeblog = FakeMW()


def test_naver_publisher_with_image(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVER_BLOG_ID", "me"); monkeypatch.setenv("NAVER_API_PASSWORD", "pw")
    c = cfg(tmp_path, blog={"publisher": "naver", "default_category": "여행"})
    (tmp_path / "img").mkdir(); (tmp_path / "img" / "a.jpg").write_bytes(b"x")
    srv = FakeServer(); pub = NaverPublisher(c, server=srv)
    db = DB(":memory:"); db.add_topic("제주 여행", {"images": ["a.jpg"]}); t = db.next_topic()
    res = process_topic(t, c, db, MockGenerator(c), pub)
    post, flag = srv.metaWeblog.posts[0]
    assert res.url == "https://blog.naver.com/me/123" and flag is True
    assert post["categories"] == ["여행"] and "https://img/a.jpg" in post["description"]
    assert srv.metaWeblog.media == ["a.jpg"]


def test_scheduler_rules(tmp_path, monkeypatch):
    c = cfg(tmp_path, schedule={"daily_limit": 2, "min_interval_minutes": 60, "jitter_minutes": 0,
                                "active_hours": [9, 22]})
    db = DB(":memory:")
    for k in ("제주도 3박4일 여행 코스", "강남역 점심 맛집 추천", "성수동 분위기 좋은 카페 모음"): db.add_topic(k)
    class Pub(FilePublisher):
        mode = "naver"
        def publish(self, *a, **k):
            r = super().publish(*a, **k); r.mode = "naver"; return r
    s = Scheduler(c, db, MockGenerator(c), Pub(c), random.Random(0))
    day = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    clock = {"t": day}
    monkeypatch.setattr("autoblog.db.now_iso", lambda: clock["t"].isoformat(timespec="seconds"))

    def tick_at(t):
        clock["t"] = t
        return s.tick(t)

    assert not tick_at(day.replace(hour=3))           # 활동 시간 외
    assert tick_at(day)                               # 1번째
    assert not tick_at(day + timedelta(minutes=5))    # 간격 미달
    assert tick_at(day + timedelta(hours=2))          # 2번째
    assert not tick_at(day + timedelta(hours=5))      # 일일 한도
