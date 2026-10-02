"""한 주제를 처리하는 파이프라인: 생성 -> 품질검사(재생성) -> HTML 변환 -> 발행 -> 기록."""
from __future__ import annotations

import logging
from pathlib import Path

from .db import DB
from .generator import Article
from .publishers import PublishResult
from .quality import check_article
from .render import render_html, render_theme

log = logging.getLogger("autoblog")


class QualityError(Exception):
    pass


def generate_checked(topic: dict, cfg: dict, generator, db: DB) -> Article:
    """품질검사를 통과할 때까지 최대 max_regenerations 회 재생성한다."""
    feedback: list[str] | None = None
    titles = db.all_titles()
    issues: list[str] = []
    for attempt in range(cfg["quality"]["max_regenerations"] + 1):
        article = generator.generate(topic, feedback)
        issues = check_article(article, topic["keyword"], cfg, titles)
        if not issues:
            return article
        log.warning("품질검사 실패(시도 %d): %s", attempt + 1, "; ".join(issues))
        feedback = issues
    raise QualityError("품질검사 미통과: " + "; ".join(issues))


def make_image_resolver(topic: dict, cfg: dict, publisher):
    base = Path(cfg["paths"]["images"])
    meta_dir = topic["meta"].get("image_dir")
    search = [Path(meta_dir)] if meta_dir else []
    search += [base / str(topic["id"]), base]

    def resolve(src: str) -> str | None:
        if src.startswith(("http://", "https://")):
            return src
        for d in [Path("."), *search]:
            p = d / src
            if p.exists():
                return publisher.upload_image(p)
        log.warning("이미지를 찾을 수 없어 생략: %s", src)
        return None

    return resolve


def process_topic(topic: dict, cfg: dict, db: DB, generator, publisher, record: bool = True) -> PublishResult:
    """record=False 면 큐 상태·이력을 건드리지 않는다(미리보기용)."""
    try:
        article = generate_checked(topic, cfg, generator, db)
        b = cfg["blog"]
        html_body = render_html(
            article.body_md,
            resolve_image=make_image_resolver(topic, cfg, publisher),
            tags=article.tags,
            theme=render_theme(cfg),
            disclosure=b["ai_disclosure_text"] if b["ai_disclosure"] else "",
        )
        res = publisher.publish(
            article.title, html_body, article.tags, topic["meta"].get("category", "")
        )
    except Exception as e:  # 실패는 기록하고 다음 주제로 넘어갈 수 있게 한다
        if record:
            db.set_status(topic["id"], "failed", error=f"{type(e).__name__}: {e}", bump_attempt=True)
        raise
    if not record:
        return res
    db.add_post(topic["id"], article.title, res.mode, res.remote_id, res.url, res.file_path)
    db.set_status(topic["id"], "done", bump_attempt=True)
    log.info("발행 완료 [%s] %s %s", res.mode, article.title, res.url or res.file_path)
    return res
