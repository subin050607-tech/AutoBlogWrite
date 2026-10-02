"""발행기.

- NaverPublisher: 네이버 블로그 공식 XML-RPC(MetaWeblog) API 사용.
    블로그 관리 > 글 API 설정에서 만든 'API 연동 비밀번호'로 인증한다(로그인 비번 아님).
- FilePublisher: HTML 파일로 저장만 한다(검수용/드라이런/수동 복붙).
"""
from __future__ import annotations

import mimetypes
import re
import xmlrpc.client
from dataclasses import dataclass
from pathlib import Path

from .config import secret


@dataclass
class PublishResult:
    remote_id: str = ""
    url: str = ""
    mode: str = "file"
    file_path: str = ""


def _slug(title: str) -> str:
    return re.sub(r"[^\w가-힣-]+", "_", title).strip("_")[:50] or "post"


class FilePublisher:
    mode = "file"

    def __init__(self, cfg: dict):
        self.out = Path(cfg["paths"]["output"])
        self.images_dir = Path(cfg["paths"]["images"])

    def upload_image(self, path: Path) -> str | None:
        return path.resolve().as_uri() if path.exists() else None

    def publish(self, title: str, html_body: str, tags: list[str], category: str = "") -> PublishResult:
        self.out.mkdir(parents=True, exist_ok=True)
        from datetime import datetime

        p = self.out / f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(title)}.html"
        doc = (f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
               f'<h1 style="font-family:sans-serif">{title}</h1>\n{html_body}')
        p.write_text(doc, encoding="utf-8")
        return PublishResult(mode="file", file_path=str(p))


class NaverPublisher:
    mode = "naver"

    def __init__(self, cfg: dict, server=None):
        b = cfg["blog"]
        self.blog_id = secret("NAVER_BLOG_ID")
        self.password = secret("NAVER_API_PASSWORD")
        self.cfg = cfg
        self.publish_flag = bool(b["publish"])
        self.default_category = b.get("default_category", "")
        self.server = server or xmlrpc.client.ServerProxy(b["endpoint"], allow_none=True)
        self._img_cache: dict[str, str] = {}

    def _auth(self) -> tuple[str, str, str]:
        return self.blog_id, self.blog_id, self.password

    def check_connection(self) -> list[dict]:
        """인증/엔드포인트 확인용: 최근 글 1개를 읽어 본다."""
        return self.server.metaWeblog.getRecentPosts(*self._auth(), 1)

    def upload_image(self, path: Path) -> str | None:
        if not path.exists():
            return None
        key = str(path.resolve())
        if key not in self._img_cache:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            res = self.server.metaWeblog.newMediaObject(
                *self._auth(), {"name": path.name, "type": mime, "bits": xmlrpc.client.Binary(path.read_bytes())}
            )
            self._img_cache[key] = res["url"]
        return self._img_cache[key]

    def publish(self, title: str, html_body: str, tags: list[str], category: str = "") -> PublishResult:
        post = {"title": title, "description": html_body, "mt_keywords": ", ".join(tags)}
        cat = category or self.default_category
        if cat:
            post["categories"] = [cat]
        post_id = self.server.metaWeblog.newPost(*self._auth(), post, self.publish_flag)
        post_id = str(post_id)
        return PublishResult(
            remote_id=post_id, url=f"https://blog.naver.com/{self.blog_id}/{post_id}", mode="naver"
        )


def make_publisher(cfg: dict):
    kind = cfg["blog"]["publisher"]
    if kind == "file":
        return FilePublisher(cfg)
    if kind == "naver":
        return NaverPublisher(cfg)
    raise ValueError(f"알 수 없는 blog.publisher: {kind}")
