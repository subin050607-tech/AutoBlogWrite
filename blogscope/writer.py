"""블로그 원고 작성: 키워드 → (상위 글 참고) → 제목·목차 → 본문 → 섹션 재작성 → 네이버 붙여넣기용 서식.

AI 는 사실을 확인할 수 없으므로 가격·수치·연락처 등은 사용자가 '필수 포함 내용'으로 준 것만 쓰게 하고,
불확실한 부분은 [확인 필요: …] 로 표시하게 한다.
"""
from __future__ import annotations

import html
import re

from .llm import complete_json

# ------------------------------------------------------------------ 옵션
PURPOSES = {
    "info": ("정보성", "궁금증을 해결하는 정보 정리형. 개념 → 핵심 정보 → 주의점 → 정리"),
    "review": ("후기", "직접 경험한 것처럼 쓰되 사용자가 준 경험 메모만 사실로 사용. 계기 → 경험 과정 → 장단점 → 추천 대상"),
    "compare": ("비교", "선택지 2개 이상 비교. 비교 기준 제시 → 항목별 비교 → 상황별 추천"),
    "howto": ("방법·사용법", "단계별 따라하기. 준비물 → 1단계, 2단계… → 자주 하는 실수"),
    "list": ("추천·리스트형", "번호 매긴 추천 목록. 선정 기준 → 항목별 소개 → 한눈에 정리"),
    "faq": ("FAQ", "자주 묻는 질문과 답 형식. 질문을 소제목으로 사용"),
}
TONES = {
    "friendly": ("친근한 말투", "~해요/~거든요 체, 독자에게 말 걸듯 편하게"),
    "pro": ("전문적인 말투", "~합니다 체, 근거 중심, 차분하고 신뢰감 있게"),
    "review": ("후기형 말투", "경험담 위주, 솔직한 느낌, ~했어요 체"),
    "daily": ("일상형 말투", "일기처럼 가볍고 자연스럽게, 짧은 문장"),
}
LENGTHS = {"short": ("짧게", 1500), "normal": ("보통", 2500), "long": ("길게", 4000)}

# 업종 패턴: 글 흐름 + 해당 업종 광고 관련 법령상 주의 표현(최종 검토는 사용자 몫)
PATTERNS = {
    "general": {"name": "일반 블로그", "flow": "", "rules": []},
    "hospital": {"name": "병·의원", "flow": "증상·고민 공감 → 원인과 일반적인 진단 과정 → 치료 방법 설명(장단점·개인차) → 치료 후 관리 → 내원 전 확인사항",
                 "rules": ["치료 효과를 보장하거나 단정하지 않는다(예: '완치', '100%')", "'최고', '유일', '1위' 같은 최상급·비교 표현을 쓰지 않는다",
                           "환자 치료 경험담이나 전후 비교를 쓰지 않는다", "부작용·개인차 가능성을 함께 안내한다(의료법상 의료광고 제한 사항)"]},
    "dental": {"name": "치과", "flow": "치아 고민 공감 → 원인 → 치료 선택지 비교(임플란트/브릿지 등) → 치료 과정과 기간 → 사후 관리",
               "rules": ["치료 결과 보장·단정 금지", "최상급·비교 표현 금지", "환자 후기·전후 사진 묘사 금지", "비용은 사용자가 준 정보만 쓰고 할인 강조 금지"]},
    "oriental": {"name": "한의원", "flow": "몸 상태 고민 공감 → 한의학적 관점 설명 → 치료 방법(침·한약 등) 일반 설명 → 생활 관리 팁",
                 "rules": ["효과 보장·단정 금지", "최상급·비교 표현 금지", "환자 경험담 금지", "과학적 근거가 불확실한 효능을 단정하지 않는다"]},
    "law": {"name": "법률(변호사)", "flow": "사례형 상황 제시 → 관련 법 조항과 쟁점 → 대응 절차와 준비 서류 → 주의할 점 → 상담이 필요한 경우",
            "rules": ["승소·결과를 보장하거나 단정하지 않는다", "'최고', '전문' 등은 근거 없이 쓰지 않는다", "법 조항은 확실한 것만, 불확실하면 [확인 필요]",
                      "개별 사건은 사실관계에 따라 달라질 수 있다는 점을 안내한다"]},
    "tax": {"name": "세무·회계", "flow": "독자 상황 제시 → 관련 세법 개념 → 계산·신고 절차 → 기한과 가산세 주의 → 절세 시 유의점",
            "rules": ["세율·기한·금액은 사용자가 준 것만 쓰고 나머지는 [확인 필요]", "절세 결과 보장 금지", "법을 회피하는 방법은 쓰지 않는다"]},
    "labor": {"name": "노무", "flow": "근로자/사업주 상황 제시 → 근로기준법상 기준 → 처리 절차 → 분쟁 시 대응 → 상담 안내",
              "rules": ["결과 보장 금지", "법 조항·금액은 확실한 것만, 불확실하면 [확인 필요]", "개별 사안마다 다를 수 있음을 안내"]},
    "realestate": {"name": "부동산(공인중개사)", "flow": "지역·매물 소개 → 입지와 생활 환경 → 시세 흐름(사용자 제공 정보만) → 장단점 → 방문 시 체크리스트",
                   "rules": ["확정되지 않은 개발 호재를 단정하지 않는다", "가격·면적은 사용자가 준 정보만 쓴다", "수익·시세 상승을 보장하지 않는다"]},
    "academy": {"name": "학원·교육", "flow": "학부모/학생 고민 공감 → 학습 방법론 → 커리큘럼 소개 → 학습 관리 방식 → 상담 안내",
                "rules": ["성적 향상·합격을 보장하지 않는다", "합격률 등 수치는 사용자가 준 것만 쓴다", "다른 학원 비방·비교 금지"]},
}

SYSTEM = """당신은 네이버 블로그 원고 전문 작가입니다. 검색으로 들어온 독자에게 실제로 도움이 되는 자연스러운 한국어 글을 씁니다.
반드시 요청한 JSON 형식 하나만 출력하세요.

공통 원칙:
- 확인할 수 없는 수치·가격·통계·연락처·URL·법 조항을 지어내지 않는다. 사용자가 준 '필수 포함 내용'만 사실로 사용한다.
- 꼭 필요한데 모르는 정보는 [확인 필요: 무엇] 으로 표시해 사용자가 채우게 한다.
- 핵심 키워드는 제목·도입부·소제목 일부에 자연스럽게 넣고, 본문 전체에 억지 반복하지 않는다(5~8회 정도).
- 모바일 가독성: 한 문단 2~4문장, 문단 사이 빈 줄. 필요한 곳에 '- ' 목록과 **굵게**를 쓴다.
- 참고 글이 주어져도 문장을 베끼지 말고, 다루는 주제 범위만 참고해 더 충실하게 쓴다.
- 광고·낚시성 표현(무조건, 대박, 100%, 최저가)을 쓰지 않는다."""


def normalize(inp: dict) -> dict:
    kw = (inp.get("keyword") or "").strip()
    if not kw:
        raise ValueError("키워드를 입력하세요.")
    if len(kw) > 60:
        raise ValueError("키워드가 너무 깁니다(60자 이내).")
    out = {
        "keyword": kw,
        "topic": (inp.get("topic") or "").strip()[:300],
        "purpose": inp.get("purpose") if inp.get("purpose") in PURPOSES else "info",
        "tone": inp.get("tone") if inp.get("tone") in TONES else "friendly",
        "length": inp.get("length") if inp.get("length") in LENGTHS else "normal",
        "pattern": inp.get("pattern") if inp.get("pattern") in PATTERNS else "general",
        "audience": (inp.get("audience") or "").strip()[:200],
        "facts": (inp.get("facts") or "").strip()[:3000],
        "banned": (inp.get("banned") or "").strip()[:500],
        "extra": (inp.get("extra") or "").strip()[:1000],
        "faq": bool(inp.get("faq")),
        "photo_marks": inp.get("photo_marks", True) is not False,
    }
    return out


def _brief(inp: dict, refs: list[dict] | None) -> str:
    p = PATTERNS[inp["pattern"]]
    lines = [f"핵심 키워드: {inp['keyword']}"]
    if inp["topic"]:
        lines.append(f"주제/방향: {inp['topic']}")
    lines.append(f"글 목적: {PURPOSES[inp['purpose']][0]} — {PURPOSES[inp['purpose']][1]}")
    lines.append(f"말투: {TONES[inp['tone']][0]} — {TONES[inp['tone']][1]}")
    lines.append(f"분량: 공백 제외 약 {LENGTHS[inp['length']][1]}자")
    if inp["audience"]:
        lines.append(f"타깃 독자: {inp['audience']}")
    if inp["pattern"] != "general":
        lines.append(f"업종: {p['name']}\n업종 글 흐름: {p['flow']}")
        lines.append("업종 주의사항(반드시 지킴):\n" + "\n".join(f"- {r}" for r in p["rules"]))
    if inp["facts"]:
        lines.append(f"필수 포함 내용(이것만 사실로 사용):\n{inp['facts']}")
    if inp["banned"]:
        lines.append(f"쓰지 말아야 할 표현: {inp['banned']}")
    if inp["extra"]:
        lines.append(f"추가 요청: {inp['extra']}")
    if refs:
        lines.append("참고 — 현재 이 키워드 상위 노출 글(베끼지 말고 다루는 범위만 참고):\n" + "\n".join(
            f"- {r['title']}: {r.get('description', '')[:120]}" for r in refs[:8]))
    return "\n".join(lines)


def plan(llm, inp: dict, refs: list[dict] | None = None) -> dict:
    inp = normalize(inp)
    user = _brief(inp, refs) + """

위 조건으로 글을 기획하세요. JSON 형식:
{"titles": ["제목 후보 5개 (키워드 포함, 25~40자, 낚시성 금지)"],
 "outline": [{"heading": "소제목", "points": ["이 섹션에서 다룰 내용 1~3개"]}],
 "hashtags": ["태그 10개, # 없이"]}
outline 은 도입부·마무리를 제외한 본문 소제목 4~7개."""
    d = complete_json(llm, SYSTEM, user)
    titles = [str(t).strip() for t in d.get("titles", []) if str(t).strip()][:5]
    outline = [{"heading": str(o.get("heading", "")).strip(), "points": [str(x) for x in o.get("points", [])][:4]}
               for o in d.get("outline", []) if isinstance(o, dict) and str(o.get("heading", "")).strip()][:8]
    if not titles or not outline:
        raise ValueError("AI가 제목/목차를 만들지 못했습니다. 다시 시도하세요.")
    return {"titles": titles, "outline": outline, "hashtags": _tags(d.get("hashtags"))}


def _tags(v) -> list[str]:
    out = []
    for t in v or []:
        t = re.sub(r"[#\s]", "", str(t))
        if t and t not in out:
            out.append(t[:30])
    return out[:15]


def _doc_rules(inp: dict) -> str:
    r = ['content 는 문단 사이 빈 줄, 목록은 "- ", 강조는 **굵게**, 인용은 "> " 만 사용(마크다운 제목 # 금지)']
    if inp["photo_marks"]:
        r.append('사진이 들어가면 좋을 위치에 단독 줄로 [사진: 어떤 사진] 을 넣는다(섹션당 0~1개)')
    return "\n".join(f"- {x}" for x in r)


def write(llm, inp: dict, title: str, outline: list[dict], refs: list[dict] | None = None) -> dict:
    inp = normalize(inp)
    title = (title or "").strip() or inp["keyword"]
    ol = "\n".join(f"{i + 1}. {o['heading']}" + (f" — {', '.join(o.get('points') or [])}" if o.get("points") else "")
                   for i, o in enumerate(outline or []))
    faq = ', "faq": [{"q": "질문", "a": "답"}] (3~5개)' if inp["faq"] else ""
    user = _brief(inp, refs) + f"""

제목: {title}
목차(이 순서와 소제목을 그대로 사용):
{ol or '(자유롭게 4~6개 구성)'}

작성 규칙:
{_doc_rules(inp)}

JSON 형식:
{{"title": "{title}", "intro": "도입부(독자 공감·문제 제기, 2~4문단)",
 "sections": [{{"heading": "소제목", "content": "본문"}}],
 "outro": "마무리(핵심 요약 + 행동 제안, 1~3문단)"{faq}, "hashtags": ["태그 10개"]}}"""
    d = complete_json(llm, SYSTEM, user)
    doc = {
        "title": str(d.get("title") or title).strip(),
        "intro": str(d.get("intro", "")).strip(),
        "sections": [{"heading": str(s.get("heading", "")).strip(), "content": str(s.get("content", "")).strip()}
                     for s in d.get("sections", []) if isinstance(s, dict)],
        "outro": str(d.get("outro", "")).strip(),
        "faq": [{"q": str(f.get("q", "")).strip(), "a": str(f.get("a", "")).strip()}
                for f in d.get("faq", []) if isinstance(f, dict) and f.get("q")] if inp["faq"] else [],
        "hashtags": _tags(d.get("hashtags")),
    }
    if not doc["sections"]:
        raise ValueError("AI가 본문을 만들지 못했습니다. 다시 시도하세요.")
    return doc


def rewrite_part(llm, inp: dict, doc: dict, part: str, instruction: str = "") -> dict:
    """part: 'intro' | 'outro' | 섹션 번호(0부터). 나머지 글 흐름을 보여주고 해당 부분만 다시 쓴다."""
    inp = normalize(inp)
    if part in ("intro", "outro"):
        current, label = doc.get(part, ""), {"intro": "도입부", "outro": "마무리"}[part]
        heading = ""
    else:
        try:
            i = int(part)
            if i < 0:
                raise IndexError
            sec = doc["sections"][i]
        except (ValueError, IndexError, KeyError, TypeError):
            raise ValueError("다시 쓸 섹션을 찾지 못했습니다.") from None
        current, label, heading = sec["content"], f"{i + 1}번째 섹션", sec["heading"]
    outline = "\n".join(f"- {s['heading']}" for s in doc.get("sections", []))
    user = _brief(inp, None) + f"""

글 제목: {doc.get('title', '')}
전체 소제목:
{outline}

다시 쓸 부분: {label}{f' (소제목: {heading})' if heading else ''}
현재 내용:
{current}

요청: {instruction or '같은 흐름을 유지하면서 더 자연스럽고 충실하게 다시 써 주세요.'}
작성 규칙:
{_doc_rules(inp)}
JSON 형식: {{"heading": "소제목(섹션일 때, 바꿀 필요 없으면 그대로)", "content": "다시 쓴 내용"}}"""
    d = complete_json(llm, SYSTEM, user)
    content = str(d.get("content", "")).strip()
    if not content:
        raise ValueError("AI가 내용을 만들지 못했습니다. 다시 시도하세요.")
    return {"heading": str(d.get("heading") or heading).strip(), "content": content}


def clean_doc(doc) -> dict:
    """화면에서 보낸 원고를 안전한 형식으로 정리."""
    if not isinstance(doc, dict) or not isinstance(doc.get("sections"), list):
        raise ValueError("원고 형식이 올바르지 않습니다.")
    st = lambda v, n=20000: str(v or "").strip()[:n]
    return {
        "title": st(doc.get("title"), 200),
        "intro": st(doc.get("intro")),
        "sections": [{"heading": st(x.get("heading"), 200), "content": st(x.get("content"))}
                     for x in doc["sections"][:20] if isinstance(x, dict)],
        "outro": st(doc.get("outro")),
        "faq": [{"q": st(f.get("q"), 300), "a": st(f.get("a"), 3000)} for f in (doc.get("faq") or [])[:10]
                if isinstance(f, dict) and f.get("q")],
        "hashtags": _tags(doc.get("hashtags")),
    }


# ------------------------------------------------------------------ 출력 형식
def to_text(doc: dict) -> str:
    """진단·텍스트 복사용 평문."""
    parts = [doc.get("intro", "")]
    for s in doc.get("sections", []):
        parts += [s["heading"], s["content"]]
    if doc.get("faq"):
        parts.append("자주 묻는 질문")
        parts += [f"Q. {f['q']}\nA. {f['a']}" for f in doc["faq"]]
    parts.append(doc.get("outro", ""))
    text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    if doc.get("hashtags"):
        text += "\n\n" + " ".join("#" + t for t in doc["hashtags"])
    return text


def count_photo_marks(doc: dict) -> int:
    blob = "\n".join([doc.get("intro", ""), doc.get("outro", "")] + [s["content"] for s in doc.get("sections", [])])
    return len(re.findall(r"^\s*\[사진:", blob, re.M))


_P = "font-size:16px;line-height:1.8;margin:0 0 16px;"


def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def _blocks(text: str) -> str:
    out, lines, i = [], (text or "").replace("\r\n", "\n").split("\n"), 0
    para: list[str] = []

    def flush():
        if para:
            out.append(f'<p style="{_P}">' + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i].strip()
        if not ln:
            flush()
        elif re.match(r"^\[사진:", ln):
            flush()
            out.append(f'<p style="{_P}text-align:center;color:#999;border:1px dashed #ccc;padding:18px;">📷 {_inline(ln)}</p>')
        elif ln.startswith("- "):
            flush()
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            out.append('<ul style="font-size:16px;line-height:1.8;margin:0 0 16px;">' +
                       "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        elif ln.startswith(">"):
            flush()
            out.append(f'<blockquote style="{_P}padding:10px 14px;border-left:4px solid #03c75a;background:#f6faf7;">'
                       f"{_inline(ln.lstrip('> '))}</blockquote>")
        else:
            para.append(ln)
        i += 1
    flush()
    return "\n".join(out)


def to_html(doc: dict) -> str:
    """네이버 스마트에디터에 붙여넣기 좋은 단순 서식 HTML(제목은 에디터 제목칸에 따로 입력)."""
    h = [_blocks(doc.get("intro", ""))]
    for s in doc.get("sections", []):
        h.append(f'<h3 style="font-size:20px;font-weight:bold;margin:32px 0 14px;">{_inline(s["heading"])}</h3>')
        h.append(_blocks(s["content"]))
    if doc.get("faq"):
        h.append('<h3 style="font-size:20px;font-weight:bold;margin:32px 0 14px;">자주 묻는 질문</h3>')
        for f in doc["faq"]:
            h.append(f'<p style="{_P}"><b>Q. {_inline(f["q"])}</b><br>A. {_inline(f["a"])}</p>')
    h.append(_blocks(doc.get("outro", "")))
    if doc.get("hashtags"):
        h.append(f'<p style="{_P}color:#03c75a;">' + " ".join("#" + html.escape(t) for t in doc["hashtags"]) + "</p>")
    return "\n".join(x for x in h if x)


def options() -> dict:
    return {"purposes": {k: v[0] for k, v in PURPOSES.items()}, "tones": {k: v[0] for k, v in TONES.items()},
            "lengths": {k: f"{v[0]} (약 {v[1]:,}자)" for k, v in LENGTHS.items()},
            "patterns": {k: v["name"] for k, v in PATTERNS.items()},
            "pattern_rules": {k: v["rules"] for k, v in PATTERNS.items()}}
