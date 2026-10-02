"""글 생성: Claude(anthropic) 또는 오프라인 mock 제공자.

모델에게 JSON 하나만 출력하도록 요구하고, 코드펜스/잡음이 섞여도 파싱한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import dataclass, field

from .config import secret


@dataclass
class Article:
    title: str
    body_md: str  # 마크다운(제한된 문법). 이미지: ![설명](경로)
    tags: list[str] = field(default_factory=list)
    summary: str = ""


SYSTEM_PROMPT = """당신은 네이버 블로그 전문 에디터입니다. 검색 사용자에게 실제로 도움이 되는 글을 씁니다.
반드시 아래 JSON 객체 하나만 출력하세요(설명·코드펜스 금지):
{"title": str, "summary": str, "tags": [str, ...], "body_md": str}

body_md 작성 규칙(마크다운 부분집합만 사용):
- 제목(title)은 H1을 쓰지 말고 title 필드에만 넣는다. 본문은 '## 소제목'으로 구분하고 필요하면 '### '를 쓴다.
- 허용 문법: 문단, ## / ###, **굵게**, - 목록, 1. 번호 목록, > 인용, --- 구분선, [링크](url), 표는 사용하지 않는다.
- 구조·소제목 개수·문단 호흡은 사용자의 '스타일 규칙'이 있으면 그것을 최우선으로 따른다. 없으면 도입부(공감·문제 제기) → 소제목 3~5개 → 정리/마무리.
- 이모지는 스타일 규칙이 허용할 때만, 규칙이 정한 위치에만 쓴다. 빈 줄로 문단을 구분한다.
- 이미지를 넣고 싶은 위치에는 사용자가 제공한 이미지 목록 중에서만 ![설명](파일명) 형태로 넣는다. 목록에 없으면 이미지 문법을 쓰지 않는다.
- 핵심 키워드는 제목, 도입부, 소제목 일부에 자연스럽게 포함하되 억지 반복 금지.
- 확인할 수 없는 통계·인용·URL을 지어내지 않는다.
tags는 5~10개(스타일 규칙이 개수를 정하면 그에 따름), '#' 없이 단어만."""


def load_example(persona: dict) -> str:
    path = persona.get("example_file")
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_user_prompt(topic: dict, cfg: dict, feedback: list[str] | None = None) -> str:
    p = cfg["persona"]
    meta = topic.get("meta", {})
    lines = [
        f"블로그 성격: {p['blog_theme']}",
        f"문체/톤: {meta.get('tone') or p['tone']}",
        f"주 독자: {p['audience']}",
        f"핵심 키워드: {topic['keyword']}",
        f"분량: 공백 제외 {p['min_chars']}~{p['max_chars']}자",
    ]
    if meta.get("notes"):
        lines.append(f"반드시 반영할 메모: {meta['notes']}")
    if meta.get("images"):
        lines.append("사용 가능한 이미지 파일: " + ", ".join(meta["images"]))
    for rule in p.get("extra_rules", []):
        lines.append(f"추가 규칙: {rule}")
    for rule in p.get("style_guide", []):
        lines.append(f"스타일 규칙: {rule}")
    if p.get("title_format") and not meta.get("title_hint"):
        lines.append(f"제목 형식(키워드만 바꿔 같은 틀로): {p['title_format']}")
    example = load_example(p)
    if example:
        lines.append("\n[문체·구성 참고용 예시 글] 말투, 문장 호흡, 소제목·이모지 쓰는 방식, 글의 흐름만 따라 하세요. "
                     "예시의 문장·소재·사례를 그대로 베끼지 마세요.\n<example>\n" + example + "\n</example>")
    if meta.get("title_hint"):
        lines.append(f"제목(그대로 사용): {meta['title_hint']}")
    if meta.get("outline"):
        lines.append("반드시 따를 목차(## 소제목 순서):\n" + "\n".join(f"- {h}" for h in meta["outline"]))
    if feedback:
        lines.append("\n[이전 초안의 문제 — 반드시 고쳐서 다시 작성]\n- " + "\n- ".join(feedback))
    lines.append("\n위 조건으로 글을 작성하세요.")
    return "\n".join(lines)


def parse_article(text: str) -> Article:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("모델 응답에서 JSON을 찾지 못했습니다.")
    data = json.loads(text[start : end + 1])
    for key in ("title", "body_md"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"응답에 '{key}' 가 없습니다.")
    tags = [str(t).lstrip("#").strip() for t in data.get("tags", []) if str(t).strip()]
    return Article(
        title=str(data["title"]).strip(),
        body_md=str(data["body_md"]).strip(),
        tags=tags,
        summary=str(data.get("summary", "")).strip(),
    )


class AnthropicGenerator:
    def __init__(self, cfg: dict):
        import anthropic  # 지연 import: mock/테스트 환경에서는 불필요

        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=secret("ANTHROPIC_API_KEY"))

    def _complete(self, system: str, user: str) -> str:
        llm = self.cfg["llm"]
        kwargs = {}
        if llm.get("temperature") is not None:  # 모델/SDK 버전에 따라 거부될 수 있어 설정했을 때만 전달
            kwargs["temperature"] = llm["temperature"]
        params = dict(model=llm["model"], max_tokens=llm["max_tokens"], system=system,
                      messages=[{"role": "user", "content": user}])
        try:
            msg = self.client.messages.create(**params, **kwargs)
        except Exception as e:
            # temperature 를 받지 않는 모델/SDK 조합이면 빼고 한 번 더 시도한다
            if "temperature" in kwargs and "temperature" in str(e):
                msg = self.client.messages.create(**params)
            else:
                raise
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def generate(self, topic: dict, feedback: list[str] | None = None) -> Article:
        return parse_article(self._complete(SYSTEM_PROMPT, build_user_prompt(topic, self.cfg, feedback)))

    def suggest_titles(self, keyword: str, n: int = 5) -> list[str]:
        return _json_list(self._complete(
            "네이버 블로그 제목 카피라이터. JSON 배열(문자열 리스트)만 출력한다.",
            f"핵심 키워드 '{keyword}'를 포함하고 과장·낚시 없이 클릭하고 싶은 블로그 제목 {n}개. "
            f"블로그 성격: {self.cfg['persona']['blog_theme']}"
            + (f"\n제목 형식(이 틀을 따르되 표현을 조금씩 달리): {self.cfg['persona']['title_format']}"
               if self.cfg["persona"].get("title_format") else "")))

    def suggest_outline(self, keyword: str, title: str = "") -> list[str]:
        return _json_list(self._complete(
            "블로그 기획자. '## 소제목' 텍스트만 담은 JSON 배열(문자열 리스트)을 출력한다. 도입/마무리 제외 소제목 4~6개.",
            f"키워드: {keyword}\n제목: {title or '(미정)'}\n독자: {self.cfg['persona']['audience']}"))

    def suggest_topics(self, theme: str, n: int) -> list[str]:
        out = self._complete(
            "블로그 키워드 기획자. JSON 배열(문자열 리스트)만 출력한다.",
            f"'{theme}' 주제로 검색 수요가 있을 법한 롱테일 블로그 글 키워드/제목 후보 {n}개. 서로 겹치지 않게.",
        )
        m = re.search(r"\[.*\]", out, re.S)
        return [str(x).strip() for x in json.loads(m.group(0))] if m else []


class OpenAICompatGenerator(AnthropicGenerator):
    """OpenAI 호환 /chat/completions 를 쓰는 제공자(Gemini·Groq·OpenRouter·Ollama 등). 추가 패키지 불필요."""

    def __init__(self, cfg: dict):
        llm = cfg["llm"]
        if not llm.get("base_url"):
            raise RuntimeError("llm.base_url 이 설정되지 않았습니다. config.yaml 을 확인하세요.")
        self.cfg = cfg
        self.base_url = llm["base_url"].rstrip("/")
        # 로컬 Ollama 처럼 키가 필요 없는 경우를 위해 api_key_env 가 비어 있으면 키 없이 호출
        env = llm.get("api_key_env", "LLM_API_KEY")
        self.api_key = secret(env, required=bool(env) and llm.get("api_key_required", True))

    def _complete(self, system: str, user: str) -> str:
        import time
        import urllib.error
        import urllib.request

        llm = self.cfg["llm"]
        payload = {"model": llm["model"], "max_tokens": llm["max_tokens"],
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if llm.get("temperature") is not None:
            payload["temperature"] = llm["temperature"]
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last = ""
        for attempt in range(4):
            req = urllib.request.Request(self.base_url + "/chat/completions", method="POST", headers=headers,
                                         data=json.dumps(payload).encode("utf-8"))
            try:
                with urllib.request.urlopen(req, timeout=llm.get("timeout", 180)) as r:
                    data = json.loads(r.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
                if e.code not in (429, 500, 502, 503, 504):  # 키 오류·잘못된 모델명 등은 재시도 무의미
                    raise RuntimeError(f"LLM 호출 실패 - {last}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"연결 실패: {e}"
            time.sleep(min(2 ** attempt * 5, 30))  # 무료 한도(분당 요청 수) 초과 대비 대기 후 재시도
        raise RuntimeError(f"LLM 호출 실패(재시도 소진) - {last}")


def _json_list(out: str) -> list[str]:
    m = re.search(r"\[.*\]", out, re.S)
    return [str(x).strip() for x in json.loads(m.group(0)) if str(x).strip()] if m else []


class MockGenerator:
    """API 키 없이 파이프라인 전체를 시험하기 위한 결정적 생성기."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def generate(self, topic: dict, feedback: list[str] | None = None) -> Article:
        kw = topic["keyword"]
        meta = topic.get("meta", {})
        heads = meta.get("outline") or [f"{kw}란 무엇인가요?", "준비할 것", "이렇게 해보세요", "마무리"]
        para = ("처음 찾아보시는 분들이 가장 궁금해하시는 내용을 차근차근 정리했어요. "
                "직접 확인하면서 느낀 점과 주의할 점을 함께 적었으니 천천히 읽어보세요. ") * 6
        imgs = topic.get("meta", {}).get("images") or []
        img_md = f"\n\n![{kw} 사진]({imgs[0]})\n" if imgs else ""
        sections = "".join(f"\n\n## {h}\n\n{para}{img_md if i == 0 else ''}" for i, h in enumerate(heads))
        body = f"{kw}에 대해 알아볼게요. {para}{sections}"
        return Article(title=meta.get("title_hint") or f"{kw} 완벽 정리, 처음이라면 꼭 읽어보세요", body_md=body,
                       tags=[kw, "정리", "가이드", "후기", "팁"], summary=f"{kw} 핵심 요약")

    def suggest_titles(self, keyword: str, n: int = 5) -> list[str]:
        fmt = self.cfg["persona"].get("title_format")
        if fmt:
            return [fmt.replace("{keyword}", keyword)] + self.suggest_titles_plain(keyword, n - 1)
        return self.suggest_titles_plain(keyword, n)

    @staticmethod
    def suggest_titles_plain(keyword: str, n: int) -> list[str]:
        forms = ["{} 완벽 정리, 처음이라면 꼭 읽어보세요", "{} 이것만 알면 끝", "{} 비교·후기 한눈에 보기",
                 "{} 시작 전 체크리스트", "{} 자주 묻는 질문 총정리"]
        return [forms[i % len(forms)].format(keyword) for i in range(n)]

    def suggest_outline(self, keyword: str, title: str = "") -> list[str]:
        return [f"{keyword}란 무엇인가요?", "준비할 것", "이렇게 해보세요", "주의할 점", "마무리"]

    def suggest_topics(self, theme: str, n: int) -> list[str]:
        return [f"{theme} 추천 {i + 1}" for i in range(n)]


def make_generator(cfg: dict):
    provider = cfg["llm"]["provider"]
    if provider == "mock":
        return MockGenerator(cfg)
    if provider == "anthropic":
        return AnthropicGenerator(cfg)
    if provider == "openai_compat":
        return OpenAICompatGenerator(cfg)
    raise ValueError(f"알 수 없는 llm.provider: {provider}")
