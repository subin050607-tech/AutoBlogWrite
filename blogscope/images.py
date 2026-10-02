"""무료 이미지 생성.

- gemini (기본): 글쓰기에 쓰는 Gemini API 키 그대로 사용. 이 키로 쓸 수 있는 이미지 생성 모델을 구글에 조회해 고른다.
  (무료 등급에서 이미지 생성 한도가 없거나 작을 수 있음)
- pollinations: 키 없이 쓰는 무료 서비스(image.pollinations.ai). 입력한 설명이 외부 서비스로 전송된다.
"""
from __future__ import annotations

import base64
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import llm

STYLES = {
    "illust": ("따뜻한 일러스트", "soft warm illustration, pastel colors, clean composition"),
    "character": ("귀여운 캐릭터", "cute simple character illustration, friendly mascot style, thick clean outlines, flat pastel colors"),
    "watercolor": ("수채화", "delicate watercolor painting, soft edges, light paper texture"),
    "flat": ("플랫 디자인", "flat vector design, minimal shapes, bold simple colors"),
    "photo": ("사진풍", "natural realistic photo style, soft daylight"),
}
ASPECTS = {"1:1": (1024, 1024), "4:3": (1152, 864), "16:9": (1344, 768), "3:4": (864, 1152)}


class ImageError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def build_prompt(desc: str, style: str, aspect: str) -> str:
    """주제를 맨 앞에 두고 스타일·금지사항을 뒤에 붙인다(이미지 모델은 앞부분을 가장 중요하게 본다)."""
    desc = (desc or "").strip()
    if not desc:
        raise ValueError("어떤 이미지를 만들지 설명을 입력하세요.")
    st = STYLES.get(style, STYLES["illust"])[1]
    return f"{desc}. {st}. No text, no letters, no captions, no logos, no watermark."


ENGLISH_SYSTEM = """You write prompts for an AI image generator. Output JSON only: {"prompt": "..."}.
Turn the Korean image description into ONE concrete English prompt (25-60 words):
main subject first, then what they are doing, setting/background, key objects, mood and lighting.
Use the blog title/keyword only as context so the picture matches the article. Do not add text or letters to the image.
Never add people, places or objects that contradict the description."""


def to_english(client, desc: str, title: str = "", keyword: str = "") -> str:
    """한국어 설명 → 구체적인 영어 프롬프트. 실패하면 빈 문자열(원문 사용)."""
    user = f"Blog title: {title}\nKeyword: {keyword}\nImage description (Korean): {desc}"
    try:
        d = llm.complete_json(client, ENGLISH_SYSTEM, user, temperature=0.4)
    except Exception:
        return ""
    out = str(d.get("prompt", "") if isinstance(d, dict) else "").strip()
    return out[:600]


class GeminiImage:
    def __init__(self, api_key: str, model: str = "", base: str = llm.GEMINI_BASE):
        if not api_key:
            raise ImageError("Gemini API 키가 없습니다. 설정에서 입력하세요.", 412)
        self.key, self.model, self.base = api_key, model, base.rstrip("/")

    def find_models(self) -> list[str]:
        req = urllib.request.Request(f"{self.base}/models?pageSize=200", headers={"x-goog-api-key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                import json
                d = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError):
            return []
        names = [str(m.get("name", "")).removeprefix("models/") for m in d.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        img = [n for n in names if "image" in n and "gemini" in n]
        return sorted(img, key=lambda n: (1 if "preview" in n else 0, n), reverse=False)

    def generate(self, prompt: str, aspect: str = "1:1") -> tuple[bytes, str]:
        models = [self.model] if self.model else self.find_models()
        if not models:
            raise ImageError("이 Gemini 키로 쓸 수 있는 이미지 생성 모델을 찾지 못했습니다. 설정에서 이미지 생성 방식을 "
                             "'Pollinations(키 없음)'으로 바꿔 보세요.", 404)
        body = {"contents": [{"role": "user", "parts": [{"text": f"{prompt} Aspect ratio {aspect}."}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
        last: Exception | None = None
        for name in models[:3]:
            try:
                d = llm._post(f"{self.base}/models/{urllib.parse.quote(name)}:generateContent",
                              {"x-goog-api-key": self.key}, body, tries=2)
            except llm.LLMError as e:
                last = e
                if e.status in (404, 429, 500, 503):
                    continue
                raise ImageError(str(e), e.status) from None
            for c in d.get("candidates") or []:
                for part in (c.get("content") or {}).get("parts") or []:
                    data = part.get("inlineData") or part.get("inline_data")
                    if data and data.get("data"):
                        return base64.b64decode(data["data"]), data.get("mimeType") or data.get("mime_type") or "image/png"
            last = ImageError("AI가 이미지를 만들지 않았습니다(설명을 바꿔 보세요).")
        if isinstance(last, llm.LLMError) and last.status == 429:
            raise ImageError("Gemini 무료 이미지 생성 한도를 넘었거나 무료 등급에서 지원하지 않습니다. 잠시 후 다시 시도하거나 "
                             "설정에서 이미지 생성 방식을 'Pollinations(키 없음)'으로 바꿔 보세요.", 429)
        raise ImageError(str(last) if last else "이미지 생성 실패", getattr(last, "status", 502) or 502)


class Pollinations:
    BASE = "https://image.pollinations.ai/prompt/"

    def __init__(self, base: str | None = None):
        self.base = base or self.BASE

    def generate(self, prompt: str, aspect: str = "1:1") -> tuple[bytes, str]:
        w, h = ASPECTS.get(aspect, ASPECTS["1:1"])
        qs = urllib.parse.urlencode({"width": w, "height": h, "nologo": "true", "seed": int(time.time()) % 100000})
        req = urllib.request.Request(self.base + urllib.parse.quote(prompt[:900]) + "?" + qs,
                                     headers={"User-Agent": "Mozilla/5.0 BlogScope"})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    ctype = r.headers.get("Content-Type", "")
                    data = r.read()
                if not ctype.startswith("image/") or len(data) < 1000:
                    raise ImageError("무료 이미지 서비스가 이미지를 돌려주지 않았습니다. 잠시 후 다시 시도하세요.", 502)
                return data, ctype.split(";")[0]
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt == 0:
                    time.sleep(5)
                    continue
                raise ImageError(f"무료 이미지 서비스 오류(HTTP {e.code}). 잠시 후 다시 시도하세요.", 502) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 0:
                    continue
                raise ImageError(f"무료 이미지 서비스에 연결하지 못했습니다: {e}", 502) from None
        raise ImageError("이미지 생성 실패", 502)


# ---------------------------------------------------------------- 저장소 (data/images/<원고번호>/)
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_NAME = re.compile(r"^img-\d{14}-\d{3}\.(png|jpg|webp)$")


def save(root: Path, doc_id: int, data: bytes, mime: str) -> str:
    d = root / str(int(doc_id))
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    for n in range(1000):
        name = f"img-{stamp}-{n:03d}{EXT.get(mime, '.png')}"
        if not (d / name).exists():
            (d / name).write_bytes(data)
            return name
    raise ImageError("이미지를 저장하지 못했습니다.")


def list_images(root: Path, doc_id: int) -> list[str]:
    d = root / str(int(doc_id))
    return sorted((p.name for p in d.glob("img-*") if _NAME.match(p.name)), reverse=True) if d.exists() else []


def path_of(root: Path, doc_id: str, name: str) -> Path | None:
    if not str(doc_id).isdigit() or not _NAME.match(name or ""):
        return None
    p = root / str(int(doc_id)) / name
    return p if p.is_file() else None


def delete(root: Path, doc_id: int, name: str) -> None:
    p = path_of(root, str(doc_id), name)
    if p:
        p.unlink()


def options() -> dict:
    return {"styles": {k: v[0] for k, v in STYLES.items()}, "aspects": list(ASPECTS)}
