"""글 생성: Claude(anthropic) 또는 오프라인 mock 제공자.

모델에게 JSON 하나만 출력하도록 요구하고, 코드펜스/잡음이 섞여도 파싱한다.
"""
from __future__ import annotations

import json
import re
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
- 도입부(공감·문제 제기) → 본문(소제목 3~5개) → 정리/마무리 구조.
- 이미지를 넣고 싶은 위치에는 사용자가 제공한 이미지 목록 중에서만 ![설명](파일명) 형태로 넣는다. 목록에 없으면 이미지 문법을 쓰지 않는다.
- 핵심 키워드는 제목, 도입부, 소제목 일부에 자연스럽게 포함하되 억지 반복 금지.
- 확인할 수 없는 통계·인용·URL을 지어내지 않는다.
tags는 5~10개, '#' 없이 단어만."""


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
        msg = self.client.messages.create(
            model=llm["model"],
            max_tokens=llm["max_tokens"],
            temperature=llm["temperature"],
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def generate(self, topic: dict, feedback: list[str] | None = None) -> Article:
        return parse_article(self._complete(SYSTEM_PROMPT, build_user_prompt(topic, self.cfg, feedback)))

    def suggest_topics(self, theme: str, n: int) -> list[str]:
        out = self._complete(
            "블로그 키워드 기획자. JSON 배열(문자열 리스트)만 출력한다.",
            f"'{theme}' 주제로 검색 수요가 있을 법한 롱테일 블로그 글 키워드/제목 후보 {n}개. 서로 겹치지 않게.",
        )
        m = re.search(r"\[.*\]", out, re.S)
        return [str(x).strip() for x in json.loads(m.group(0))] if m else []


class MockGenerator:
    """API 키 없이 파이프라인 전체를 시험하기 위한 결정적 생성기."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def generate(self, topic: dict, feedback: list[str] | None = None) -> Article:
        kw = topic["keyword"]
        para = ("처음 찾아보시는 분들이 가장 궁금해하시는 내용을 차근차근 정리했어요. "
                "직접 확인하면서 느낀 점과 주의할 점을 함께 적었으니 천천히 읽어보세요. ") * 6
        imgs = topic.get("meta", {}).get("images") or []
        img_md = f"\n\n![{kw} 사진]({imgs[0]})\n" if imgs else ""
        body = (f"{kw}에 대해 알아볼게요. {para}\n\n## {kw}란 무엇인가요?\n\n{para}{img_md}\n\n## 준비할 것\n\n- 기본 정보 확인\n- 비용 비교\n"
                f"- 후기 살펴보기\n\n{para}\n\n## 이렇게 해보세요\n\n1. 목표 정하기\n2. 비교하기\n3. 실행하기\n\n{para}\n\n"
                f"## 마무리\n\n{para}")
        return Article(title=f"{kw} 완벽 정리, 처음이라면 꼭 읽어보세요", body_md=body,
                       tags=[kw, "정리", "가이드", "후기", "팁"], summary=f"{kw} 핵심 요약")

    def suggest_topics(self, theme: str, n: int) -> list[str]:
        return [f"{theme} 추천 {i + 1}" for i in range(n)]


def make_generator(cfg: dict):
    provider = cfg["llm"]["provider"]
    if provider == "mock":
        return MockGenerator(cfg)
    if provider == "anthropic":
        return AnthropicGenerator(cfg)
    raise ValueError(f"알 수 없는 llm.provider: {provider}")
