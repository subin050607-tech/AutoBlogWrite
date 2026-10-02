"""설정 로딩: config.yaml + .env(환경변수) -> dict. 누락된 값은 기본값으로 채운다."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "llm": {"provider": "anthropic", "model": "claude-sonnet-5-5", "max_tokens": 6000, "temperature": 0.8},
    "blog": {
        "publisher": "naver",
        "endpoint": "https://api.blog.naver.com/xmlrpc",
        "default_category": "",
        "publish": True,
        "ai_disclosure": True,
        "heading_style": "bar",     # bar(좌측 초록 막대) | plain(이모지 소제목용, 장식 없음)
        "footer_lines": [],         # 글 끝에 가운데 정렬로 붙는 고정 문구
        "ai_disclosure_text": "※ 이 글은 AI의 도움을 받아 작성되었으며, 게시 전 직접 검토했습니다.",
    },
    "persona": {
        "blog_theme": "일상 정보 블로그",
        "tone": "친근하고 신뢰감 있는 존댓말",
        "audience": "해당 주제를 처음 검색해 보는 일반 독자",
        "min_chars": 1500,
        "max_chars": 5000,
        "extra_rules": [],
        "style_guide": [],          # 글 구성/말투 규칙(프롬프트에 그대로 전달)
        "title_format": "",         # 예: "{keyword} 특징 총정리｜..." (비우면 자유 형식)
        "example_file": "",         # 문체 참고용 예시 글 경로
    },
    "quality": {
        "require_keyword_in_title": True,
        "max_keyword_density": 0.04,
        "min_headings": 2,
        "title_similarity_limit": 0.8,
        "banned_words": [],
        "max_regenerations": 2,
    },
    "schedule": {
        "active_hours": [9, 22],
        "daily_limit": 2,
        "min_interval_minutes": 180,
        "jitter_minutes": 40,
        "poll_seconds": 60,
        "max_retries": 2,
    },
    "paths": {"db": "data/autoblog.db", "output": "output", "images": "images"},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(path: str | Path = ".env") -> None:
    """의존성 없이 .env 를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | Path = "config.yaml") -> dict:
    load_dotenv()
    p = Path(path)
    user = {}
    if p.exists():
        user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return _merge(DEFAULTS, user)


def secret(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "")
    if required and not v:
        raise RuntimeError(f"환경변수 {name} 이(가) 설정되지 않았습니다. .env 파일을 확인하세요.")
    return v
