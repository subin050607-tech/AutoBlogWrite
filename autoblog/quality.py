"""발행 전 품질 검사. 문제 목록을 반환하면 생성기에 피드백으로 되돌려 재생성한다."""
from __future__ import annotations

import difflib
import re

from .generator import Article
from .render import plain_text


def check_article(article: Article, keyword: str, cfg: dict, existing_titles: list[str]) -> list[str]:
    q, p = cfg["quality"], cfg["persona"]
    issues: list[str] = []
    text = plain_text(article.body_md)
    chars = len(re.sub(r"\s", "", text))

    if chars < p["min_chars"]:
        issues.append(f"본문이 너무 짧습니다({chars}자). 최소 {p['min_chars']}자 이상으로 더 풍부하게 쓰세요.")
    if chars > p["max_chars"]:
        issues.append(f"본문이 너무 깁니다({chars}자). {p['max_chars']}자 이내로 줄이세요.")

    headings = len(re.findall(r"^#{2,3}\s+", article.body_md, flags=re.M))
    if headings < q["min_headings"]:
        issues.append(f"소제목(##)이 {headings}개뿐입니다. 최소 {q['min_headings']}개 이상 사용하세요.")

    if q["require_keyword_in_title"] and keyword.replace(" ", "") not in article.title.replace(" ", ""):
        issues.append(f"제목에 핵심 키워드 '{keyword}'가 포함되어야 합니다.")

    if chars:
        density = text.replace(" ", "").count(keyword.replace(" ", "")) * len(keyword.replace(" ", "")) / chars
        if density > q["max_keyword_density"]:
            issues.append(f"키워드 '{keyword}' 반복이 과합니다(밀도 {density:.1%}). 동의어·대명사로 바꾸세요.")

    for w in q["banned_words"]:
        if w and (w in text or w in article.title):
            issues.append(f"금지어 '{w}'가 포함되어 있습니다. 제거하세요.")

    for t in existing_titles:
        if difflib.SequenceMatcher(None, t, article.title).ratio() >= q["title_similarity_limit"]:
            issues.append(f"기존 글 제목('{t}')과 너무 비슷합니다. 다른 제목으로 바꾸세요.")
            break

    if not article.tags:
        issues.append("태그가 비어 있습니다.")
    return issues
