"""API 키 설정: data/settings.json (화면의 '설정'에서 입력). 같은 이름의 환경변수가 있으면 그 값이 우선한다."""
from __future__ import annotations

import json
import os
from pathlib import Path

FIELDS = {
    "naver_api_source": "NAVER_API_SOURCE",    # hub(NAVER API HUB, 기본) | developers(기존 개발자센터 키)
    "naver_client_id": "NAVER_CLIENT_ID",
    "naver_client_secret": "NAVER_CLIENT_SECRET",
    "searchad_api_key": "SEARCHAD_API_KEY",
    "searchad_secret": "SEARCHAD_SECRET",
    "searchad_customer_id": "SEARCHAD_CUSTOMER_ID",
    "llm_provider": "LLM_PROVIDER",          # gemini(기본) | openai_compat
    "gemini_api_key": "GEMINI_API_KEY",
    "llm_model": "LLM_MODEL",                # 비우면 gemini-3.8-flash
    "llm_base_url": "LLM_BASE_URL",          # openai_compat 일 때
    "llm_api_key": "LLM_API_KEY",            # openai_compat 일 때
    "image_provider": "IMAGE_PROVIDER",      # gemini(기본) | pollinations(키 없음)
    "image_model": "IMAGE_MODEL",            # 비우면 자동 선택
    "my_blog_id": "MY_BLOG_ID",              # 네이버로 보내기: 내 블로그 글쓰기 창 주소
    "nickname": "BLOG_NICKNAME",             # 글 스타일: '○○의 한마디'
    "signature": "BLOG_SIGNATURE",           # 글 끝 고정 인사말(여러 줄)
    "style_example": "STYLE_EXAMPLE",        # '내 글 스타일' 예시 글
}
SECRET = {"naver_client_secret", "searchad_api_key", "searchad_secret", "gemini_api_key", "llm_api_key"}


class Settings:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def get(self, name: str) -> str:
        return (os.environ.get(FIELDS[name]) or self.data.get(name) or "").strip()

    def update(self, values: dict) -> None:
        for k, v in values.items():
            if k in FIELDS and isinstance(v, str) and v.strip() != "":
                self.data[k] = v.strip()[:8000]
            elif k in ("my_blog_id", "nickname", "signature", "style_example", "image_model", "llm_model") and v == "":
                self.data.pop(k, None)  # 공개 항목은 빈 값으로 지울 수 있다
            elif k in FIELDS and v is None:  # 명시적 삭제
                self.data.pop(k, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def public(self) -> dict:
        """화면 표시용: 비밀값은 앞 4자리만."""
        out = {}
        for k in FIELDS:
            v = self.get(k)
            out[k] = (v[:4] + "•" * 6 if v and k in SECRET else v)
        out["naver_api_source"] = "developers" if self.get("naver_api_source") == "developers" else "hub"
        out["image_provider"] = "pollinations" if self.get("image_provider") == "pollinations" else "gemini"
        out["llm_provider"] = "openai_compat" if self.get("llm_provider") == "openai_compat" else "gemini"
        out["has_llm"] = bool(self.get("gemini_api_key")) if out["llm_provider"] == "gemini" else \
            bool(self.get("llm_base_url") and self.get("llm_model"))
        out["has_search"] = bool(self.get("naver_client_id") and self.get("naver_client_secret"))
        out["has_searchad"] = all(self.get(k) for k in ("searchad_api_key", "searchad_secret", "searchad_customer_id"))
        return out
