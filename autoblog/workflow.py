"""글 워크플로: 초안(draft) → 검수완료(reviewed) → 예약(scheduled) → 발행완료(published) / 실패(failed).

사용자가 제목·목차를 고르고, 초안을 편집·검수한 뒤 승인해야 발행된다(자동 생성 글의 무검토 발행 방지).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .db import DB
from .generator import Article
from .pipeline import make_image_resolver
from .quality import check_article
from .render import render_html

log = logging.getLogger("autoblog")

RETRY_DELAY_MIN = 10


class WorkflowError(Exception):
    pass


def _topic_for(db: DB, article: dict) -> dict:
    t = db.get_topic(article["topic_id"]) if article["topic_id"] else None
    return t or {"id": 0, "keyword": article["title"], "meta": {}}


def review(db: DB, cfg: dict, article_id: int) -> list[str]:
    """품질 검수를 돌려 이슈를 저장한다. 이슈가 없으면 reviewed, 있으면 draft 유지."""
    a = _get(db, article_id)
    t = _topic_for(db, a)
    art = Article(a["title"], a["body_md"], a["tags"], a["summary"])
    titles = [x for x in db.all_titles() if x != a["title"]]
    issues = check_article(art, t["keyword"], cfg, titles)
    # 이미지 누락(본문이 참조하는 파일이 없음)은 resolver 가 경고만 하므로 여기서 한 번 더 점검
    issues += _image_issues(a, t, cfg)
    if a["status"] in ("draft", "reviewed", "failed"):
        db.update_article(article_id, issues=issues, status="draft" if issues else "reviewed")
    else:
        db.update_article(article_id, issues=issues)
    return issues


def _image_issues(a: dict, topic: dict, cfg: dict) -> list[str]:
    import re
    from pathlib import Path

    out = []
    base = Path(cfg["paths"]["images"])
    dirs = [Path("."), base / str(topic["id"]), base]
    if topic["meta"].get("image_dir"):
        dirs.insert(0, Path(topic["meta"]["image_dir"]))
    for src in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", a["body_md"]):
        if src.startswith(("http://", "https://")):
            continue
        if not any((d / src).exists() for d in dirs):
            out.append(f"이미지 파일을 찾을 수 없습니다: {src}")
    return out


def _get(db: DB, article_id: int) -> dict:
    a = db.get_article(article_id)
    if not a:
        raise WorkflowError(f"글 #{article_id} 없음")
    return a


def create_draft(db: DB, cfg: dict, generator, topic_id: int, title: str = "", outline: list[str] | None = None) -> int:
    """주제로부터 초안을 생성해 저장한다(발행하지 않음). 품질 이슈는 저장만 하고 사용자가 판단한다."""
    topic = db.get_topic(topic_id)
    if not topic:
        raise WorkflowError(f"주제 #{topic_id} 없음")
    meta = dict(topic["meta"])
    if title:
        meta["title_hint"] = title
    if outline:
        meta["outline"] = outline
    art = generator.generate({**topic, "meta": meta})
    aid = db.add_article(topic_id, title or art.title, art.body_md, art.tags, art.summary, outline or [])
    db.set_status(topic_id, "drafted")
    review(db, cfg, aid)
    return aid


def regenerate(db: DB, cfg: dict, generator, article_id: int) -> list[str]:
    a = _get(db, article_id)
    if a["status"] in ("scheduled", "published"):
        raise WorkflowError("예약/발행된 글은 재생성할 수 없습니다. 예약을 취소한 뒤 다시 시도하세요.")
    topic = _topic_for(db, a)
    meta = {**topic["meta"], "title_hint": a["title"], "outline": a["outline"]}
    art = generator.generate({**topic, "meta": meta}, a["issues"] or None)
    db.update_article(article_id, title=art.title, body_md=art.body_md, tags=art.tags, summary=art.summary)
    return review(db, cfg, article_id)


def save_edit(db: DB, cfg: dict, article_id: int, **fields) -> list[str]:
    """임시저장. 편집하면 검수 결과는 무효가 되므로 다시 검수한다(예약 글은 draft 로 되돌림)."""
    a = _get(db, article_id)
    if a["status"] == "published":
        raise WorkflowError("발행 완료된 글은 수정할 수 없습니다.")
    allowed = {k: v for k, v in fields.items() if k in ("title", "body_md", "tags", "summary", "outline")}
    allowed["status"] = "draft"
    allowed["scheduled_at"] = None
    db.update_article(article_id, **allowed)
    return review(db, cfg, article_id)


def schedule(db: DB, article_id: int, when: datetime | None) -> None:
    """when=None 이면 즉시 발행 대기열(지금)에 올린다. 검수 통과(reviewed)한 글만 가능."""
    a = _get(db, article_id)
    if a["status"] not in ("reviewed", "scheduled", "failed"):
        raise WorkflowError("검수를 통과한 글만 예약할 수 있습니다. 먼저 '검수'를 실행하세요.")
    if a["issues"]:
        raise WorkflowError("검수 이슈가 남아 있습니다: " + "; ".join(a["issues"]))
    db.update_article(article_id, status="scheduled", retries=0, last_error=None,
                      scheduled_at=(when or datetime.now()).isoformat(timespec="seconds"))


def cancel_schedule(db: DB, article_id: int) -> None:
    a = _get(db, article_id)
    if a["status"] != "scheduled":
        raise WorkflowError("예약 상태가 아닙니다.")
    db.update_article(article_id, status="reviewed", scheduled_at=None)


def publish(db: DB, cfg: dict, publisher, article_id: int) -> str:
    """승인된 글을 발행한다. 실패하면 failed 로 기록하고 예외를 다시 던진다."""
    a = _get(db, article_id)
    if a["status"] == "published":
        raise WorkflowError("이미 발행된 글입니다.")
    if a["status"] not in ("reviewed", "scheduled", "failed"):
        raise WorkflowError("검수를 통과해야 발행할 수 있습니다.")
    t = _topic_for(db, a)
    try:
        b = cfg["blog"]
        body = render_html(a["body_md"], resolve_image=make_image_resolver(t, cfg, publisher), tags=a["tags"],
                           disclosure=b["ai_disclosure_text"] if b["ai_disclosure"] else "")
        res = publisher.publish(a["title"], body, a["tags"], t["meta"].get("category", ""))
    except Exception as e:
        db.update_article(article_id, status="failed", last_error=f"{type(e).__name__}: {e}",
                          retries=a["retries"] + 1)
        raise
    pid = db.add_post(a["topic_id"], a["title"], res.mode, res.remote_id, res.url, res.file_path)
    db.update_article(article_id, status="published", post_id=pid, last_error=None, scheduled_at=None)
    if a["topic_id"]:
        db.set_status(a["topic_id"], "done", bump_attempt=True)
    log.info("발행 완료 [%s] %s %s", res.mode, a["title"], res.url or res.file_path)
    return res.url or res.file_path


def publish_due(db: DB, cfg: dict, publisher_factory, now: datetime | None = None) -> int:
    """예약 시각이 지난 글을 발행한다. 실패하면 재시도 한도까지 RETRY_DELAY_MIN 분 뒤로 미룬다."""
    now = now or datetime.now()
    max_retries = cfg["schedule"].get("max_retries", 2)
    n = 0
    for a in db.due_articles(now.isoformat(timespec="seconds")):
        try:
            publish(db, cfg, publisher_factory(), a["id"])
            n += 1
        except Exception as e:
            log.error("예약 발행 실패 #%s: %s", a["id"], e)
            cur = db.get_article(a["id"])
            if cur["retries"] <= max_retries:
                db.update_article(a["id"], status="scheduled",
                                  scheduled_at=(now + timedelta(minutes=RETRY_DELAY_MIN)).isoformat(timespec="seconds"))
    return n
