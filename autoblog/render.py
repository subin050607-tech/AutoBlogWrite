"""마크다운 부분집합 -> 네이버 블로그용 HTML.

네이버는 <style>/class 를 제거하는 경우가 많아서 인라인 스타일만 사용한다.
이미지 경로는 resolve_image(src) 콜백으로 (업로드된) URL 로 바꾼다.
"""
from __future__ import annotations

import html
import re
from typing import Callable

FONT = "font-family:'Nanum Gothic',sans-serif;"
STYLES = {
    "p": f"{FONT}font-size:16px;line-height:1.9;margin:0 0 18px;",
    "h2": f"{FONT}font-size:22px;font-weight:bold;margin:36px 0 14px;padding-left:10px;border-left:5px solid #03c75a;",
    "h3": f"{FONT}font-size:18px;font-weight:bold;margin:26px 0 10px;",
    "ul": f"{FONT}font-size:16px;line-height:1.9;margin:0 0 18px;padding-left:24px;",
    "quote": f"{FONT}font-size:16px;line-height:1.8;margin:0 0 18px;padding:12px 16px;background:#f5f7f6;border-left:4px solid #ccc;",
    "hr": "border:0;border-top:1px solid #ddd;margin:28px 0;",
    "img": "max-width:100%;display:block;margin:18px auto;",
    "caption": f"{FONT}font-size:13px;color:#888;text-align:center;margin:-8px 0 18px;",
    "disclosure": f"{FONT}font-size:13px;color:#888;margin-top:32px;",
    "tags": f"{FONT}font-size:14px;color:#03c75a;margin-top:18px;",
}

_IMG = re.compile(r"^!\[(.*?)\]\((.+?)\)\s*$")


def _inline(text: str) -> str:
    s = html.escape(text, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(
        r"\[(.+?)\]\((https?://[^\s)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" target="_blank">{m.group(1)}</a>',
        s,
    )
    return s


def render_html(
    body_md: str,
    resolve_image: Callable[[str], str | None] | None = None,
    tags: list[str] | None = None,
    disclosure: str = "",
) -> str:
    out: list[str] = []
    para: list[str] = []
    lines = body_md.replace("\r\n", "\n").split("\n")

    def flush_para() -> None:
        if para:
            out.append(f'<p style="{STYLES["p"]}">' + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
        elif stripped.startswith("### "):
            flush_para(); out.append(f'<h3 style="{STYLES["h3"]}">{_inline(stripped[4:])}</h3>')
        elif stripped.startswith("## ") or stripped.startswith("# "):
            flush_para(); out.append(f'<h2 style="{STYLES["h2"]}">{_inline(stripped.lstrip("# "))}</h2>')
        elif re.fullmatch(r"-{3,}", stripped):
            flush_para(); out.append(f'<hr style="{STYLES["hr"]}">')
        elif _IMG.match(stripped):
            flush_para()
            alt, src = _IMG.match(stripped).groups()
            url = resolve_image(src) if resolve_image else src
            if url:
                out.append(f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(alt, quote=True)}" style="{STYLES["img"]}">')
                if alt:
                    out.append(f'<p style="{STYLES["caption"]}">{html.escape(alt)}</p>')
            # url 이 None 이면(파일 없음) 이미지는 조용히 생략
        elif re.match(r"^([-*]|\d+\.)\s+", stripped):
            flush_para()
            ordered = bool(re.match(r"^\d+\.", stripped))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i]).strip())
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f'<{tag} style="{STYLES["ul"]}">' + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        elif stripped.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f'<blockquote style="{STYLES["quote"]}">' + "<br>".join(_inline(x) for x in quote) + "</blockquote>")
            continue
        else:
            para.append(stripped)
        i += 1
    flush_para()

    if disclosure:
        out.append(f'<p style="{STYLES["disclosure"]}">{html.escape(disclosure)}</p>')
    if tags:
        out.append(f'<p style="{STYLES["tags"]}">' + " ".join("#" + html.escape(t.replace(" ", "")) for t in tags) + "</p>")
    return "\n".join(out)


def plain_text(body_md: str) -> str:
    """품질 검사용: 마크다운 기호 제거."""
    s = re.sub(r"!\[.*?\]\(.*?\)", "", body_md)
    s = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", s)
    s = re.sub(r"^\s*(#{1,6}|>|[-*]|\d+\.)\s+", "", s, flags=re.M)
    return re.sub(r"[*_`]", "", s)
