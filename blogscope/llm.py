"""무료로 쓸 수 있는 LLM 연결.

- gemini (기본): Google AI Studio 무료 키 (https://aistudio.google.com/apikey, 카드 불필요, 분당/일일 한도 있음)
- openai_compat: OpenAI 호환 /chat/completions (Groq, OpenRouter 무료 모델, 내 PC의 Ollama 등)

모델에게 JSON 하나만 출력하게 하고, 잡음이 섞여도 파싱한다.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"  # 2.5-flash 는 신규 사용자에게 제공 중단(구글 API 안내)


class LLMError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _post(url: str, headers: dict, body: dict, timeout: int = 180, tries: int = 5) -> dict:
    data = json.dumps(body).encode("utf-8")
    last = ""
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                last = f"HTTP {e.code}"
                time.sleep(min(5 * 2 ** attempt, 30))  # 한도 초과·서버 혼잡 → 잠시 대기 후 재시도(최대 약 1분)
                continue
            hint = {400: "요청 오류(모델 이름을 확인하세요)", 401: "API 키가 올바르지 않습니다", 403: "API 키 권한이 없습니다",
                    404: "모델을 찾을 수 없습니다(설정의 모델 이름 확인)",
                    429: "무료 사용 한도를 넘었습니다. 1~2분 뒤(일일 한도라면 내일) 다시 시도하세요",
                    503: "구글 AI 서버가 일시적으로 혼잡합니다. 1~2분 뒤 다시 시도하세요. 계속되면 설정에서 다른 모델로 바꿔 보세요",
                    500: "구글 AI 서버 오류입니다. 잠시 뒤 다시 시도하세요"}.get(e.code, "AI 호출 실패")
            raise LLMError(f"{hint} (HTTP {e.code})" + ("" if e.code in (429, 500, 503) else f" {text}"), e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(e)
            if attempt < 3:
                time.sleep(2)
                continue
    raise LLMError(f"AI 서버에 연결하지 못했습니다: {last}")


def parse_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    for open_, close in (("{", "}"), ("[", "]")):
        a, b = text.find(open_), text.rfind(close)
        if a >= 0 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("AI 응답을 해석하지 못했습니다. 다시 시도하세요.")


class Gemini:
    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL, base: str = GEMINI_BASE):
        if not api_key:
            raise LLMError("Gemini API 키가 없습니다. '설정' 탭에서 입력하세요.", 412)
        self.key, self.model, self.base = api_key, model or DEFAULT_GEMINI_MODEL, base.rstrip("/")
        self.switched_to: str | None = None
        self.used_model: str | None = None

    def complete(self, system: str, user: str, json_mode: bool = True, temperature: float = 0.8) -> str:
        cfg = {"temperature": temperature, "maxOutputTokens": 16384}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": cfg}
        try:
            d = _post(self._url(), {"x-goog-api-key": self.key}, body, tries=3)
        except LLMError as e:
            # 구글이 모델을 내리면 404 메시지에 대체 모델을 알려준다("Please update your code to use models/xxx").
            m = re.search(r"use models/([A-Za-z0-9._-]+)", str(e))
            if e.status == 404 and m and m.group(1) != self.model:
                self.model = self.switched_to = m.group(1)
                d = _post(self._url(), {"x-goog-api-key": self.key}, body, tries=3)
            elif e.status in (429, 500, 503):
                d = self._fallback(body, e)
            else:
                raise
        return self._text(d)

    def list_models(self) -> list[str]:
        """이 키로 글 생성(generateContent)이 가능한 모델 이름 목록(구글에 직접 조회)."""
        req = urllib.request.Request(f"{self.base}/models?pageSize=200", headers={"x-goog-api-key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError):
            return []
        out = []
        for m in d.get("models", []):
            name = str(m.get("name", "")).removeprefix("models/")
            if "generateContent" in (m.get("supportedGenerationMethods") or []) and "gemini" in name \
                    and not any(x in name for x in ("embedding", "image", "tts", "audio", "live", "vision", "native")):
                out.append(name)
        return out

    def _fallback(self, body: dict, err: LLMError) -> dict:
        """지금 모델이 혼잡/한도 초과면 같은 키로 쓸 수 있는 다른 모델로 한 번씩 시도한다."""
        rank = lambda n: (0 if "flash" in n and "lite" not in n else 1 if "flash" in n else 2,
                          1 if any(x in n for x in ("preview", "exp")) else 0)
        tried = {self.model}
        for name in sorted((n for n in self.list_models() if n not in tried), key=rank)[:4]:
            try:
                d = _post(f"{self.base}/models/{urllib.parse.quote(name)}:generateContent",
                          {"x-goog-api-key": self.key}, body, tries=2)
            except LLMError as e:
                if e.status in (429, 500, 503, 404):
                    continue
                raise
            self.used_model = name  # 이번 한 번만 대체(설정은 유지)
            return d
        raise err

    def _url(self) -> str:
        return f"{self.base}/models/{urllib.parse.quote(self.model)}:generateContent"

    @staticmethod
    def _text(d: dict) -> str:
        cands = d.get("candidates") or []
        if not cands:
            reason = (d.get("promptFeedback") or {}).get("blockReason", "알 수 없음")
            raise LLMError(f"AI가 응답을 거부했습니다(사유: {reason}). 주제나 표현을 바꿔 보세요.")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise LLMError(f"AI 응답이 비어 있습니다(finishReason: {cands[0].get('finishReason')}). 다시 시도하세요.")
        return text


class OpenAICompat:
    def __init__(self, base_url: str, api_key: str, model: str):
        if not base_url or not model:
            raise LLMError("OpenAI 호환 설정(주소·모델)이 비어 있습니다.", 412)
        self.base, self.key, self.model = base_url.rstrip("/"), api_key, model

    def complete(self, system: str, user: str, json_mode: bool = True, temperature: float = 0.8) -> str:
        body = {"model": self.model, "temperature": temperature, "max_tokens": 8192,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        d = _post(f"{self.base}/chat/completions", {"Authorization": f"Bearer {self.key}"} if self.key else {}, body)
        try:
            return d["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise LLMError("AI 응답 형식이 올바르지 않습니다.") from None


def complete_json(client, system: str, user: str, temperature: float = 0.8):
    """JSON 파싱 실패 시 한 번 재요청."""
    try:
        return parse_json(client.complete(system, user, True, temperature))
    except LLMError as e:
        if "해석" not in str(e):
            raise
        return parse_json(client.complete(system, user + "\n\n반드시 유효한 JSON 하나만 출력하세요.", True, temperature))
