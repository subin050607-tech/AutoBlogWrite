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
}
SECRET = {"naver_client_secret", "searchad_api_key", "searchad_secret"}


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
                self.data[k] = v.strip()
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
        out["has_search"] = bool(self.get("naver_client_id") and self.get("naver_client_secret"))
        out["has_searchad"] = all(self.get(k) for k in ("searchad_api_key", "searchad_secret", "searchad_customer_id"))
        return out
