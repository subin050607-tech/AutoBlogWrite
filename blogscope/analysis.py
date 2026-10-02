"""분석 로직. 모든 점수는 공개 데이터로 계산한 '자체 추정치'이며 네이버 공식 지수가 아니다.

계산식을 숨기지 않고 결과에 항목별 점수·근거를 함께 담는다.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev

from .naver import NaverError, OpenAPI, SearchAd, parse_blog_ref

KST = timezone(timedelta(hours=9))
DISCLAIMER = ("네이버는 공식 '블로그 지수'를 공개하지 않습니다. 이 점수는 공개 RSS와 네이버 검색 API 결과로 계산한 "
              "자체 추정치이며, 실제 검색 순위를 보장하지 않습니다.")


def now_kst() -> datetime:
    return datetime.now(KST)


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(KST).isoformat(timespec="minutes") if d else None


def _ratio_score(x: float, lo: float, hi: float) -> float:
    """x 가 lo 이하면 0, hi 이상이면 100 인 선형 점수."""
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))


# ================================================================ 블로그 분석
def blog_metrics(rss: dict, now: datetime) -> dict:
    posts = sorted((p for p in rss["posts"] if p["date"]), key=lambda p: p["date"], reverse=True)
    dates = [p["date"] for p in posts]
    age = lambda d: (now - d).total_seconds() / 86400
    intervals = [(dates[i] - dates[i + 1]).total_seconds() / 86400 for i in range(min(len(dates), 20) - 1)]
    avg_iv = mean(intervals) if intervals else None
    cv = (pstdev(intervals) / avg_iv) if intervals and len(intervals) >= 2 and avg_iv > 0 else None

    months = []
    for k in range(5, -1, -1):  # 최근 6개월 월별 발행 수
        y, m = now.year, now.month - k
        while m <= 0:
            y, m = y - 1, m + 12
        months.append({"label": f"{y % 100:02d}.{m:02d}",
                       "value": sum(1 for d in dates if d.astimezone(KST).year == y and d.astimezone(KST).month == m)})
    wd = Counter(d.astimezone(KST).weekday() for d in dates)
    hr = Counter(d.astimezone(KST).hour for d in dates)
    return {
        "posts_in_rss": len(posts),
        "recent7": sum(1 for d in dates if age(d) < 7),
        "recent30": sum(1 for d in dates if age(d) < 30),
        "last_post_days": round(age(dates[0]), 1) if dates else None,
        "avg_interval_days": round(avg_iv, 1) if avg_iv is not None else None,
        "interval_cv": round(cv, 2) if cv is not None else None,
        "monthly": months,
        "weekday": [{"label": "월화수목금토일"[i], "value": wd.get(i, 0)} for i in range(7)],
        "hours": [{"label": f"{h}", "value": hr.get(h, 0)} for h in range(24)],
        "categories": [{"label": c or "(없음)", "value": n} for c, n in Counter(p["category"] for p in posts).most_common(8)],
        "top_tags": [{"label": t, "value": n} for t, n in Counter(t for p in posts for t in p["tags"]).most_common(15)],
    }


def _same_post(link: str, blog_id: str, log_no: str | None) -> bool:
    try:
        bid, ln = parse_blog_ref(link)
    except ValueError:
        return False
    return bid == blog_id.lower() and (log_no is None or ln == log_no)


def exposure_check(api: OpenAPI, blog_id: str, posts: list[dict], now: datetime, limit: int = 10) -> list[dict]:
    """최근 글 제목을 그대로 검색해 내 글이 몇 위에 뜨는지 확인(누락 의심 글 찾기)."""
    out = []
    for p in posts[:limit]:
        row = {"title": p["title"], "link": p["link"], "date": _iso(p["date"]), "rank": None, "status": ""}
        if p["date"] and (now - p["date"]) < timedelta(hours=12):
            row["status"] = "pending"  # 발행 직후는 색인 전일 수 있어 판정 보류
            out.append(row)
            continue
        if len(p["title"]) < 2:
            continue
        try:
            res = api.search_blog(p["title"], display=100, sort="sim")
        except NaverError as e:
            if e.status in (401, 403):
                raise
            row["status"] = "error"
            out.append(row)
            continue
        for i, it in enumerate(res["items"], 1):
            if _same_post(it["link"], blog_id, p["log_no"]):
                row["rank"] = i
                break
        row["status"] = "top10" if row["rank"] and row["rank"] <= 10 else ("found" if row["rank"] else "missing")
        out.append(row)
    return out


def _exposure_score(rows: list[dict]) -> float | None:
    judged = [r for r in rows if r["status"] in ("top10", "found", "missing")]
    if not judged:
        return None
    pts = [1.0 if r["rank"] and r["rank"] <= 3 else 0.85 if r["rank"] and r["rank"] <= 10 else
           0.5 if r["rank"] and r["rank"] <= 30 else 0.25 if r["rank"] else 0.0 for r in judged]
    return mean(pts) * 100


GRADES = [(90, "S", "최상위"), (75, "A", "상위"), (60, "B", "중위"), (40, "C", "하위"), (0, "D", "저활동")]


def grade_of(score: float) -> dict:
    for cut, g, name in GRADES:
        if score >= cut:
            return {"grade": g, "name": name}
    return {"grade": "D", "name": "저활동"}


def score_blog(m: dict, exposure_rows: list[dict] | None) -> dict:
    comps = []
    comps.append({"name": "활동량", "weight": 30, "score": _ratio_score(m["recent30"], 0, 12),
                  "basis": f"최근 30일 발행 {m['recent30']}개 (12개 이상 만점)"})
    if m["interval_cv"] is None:
        cons, basis = 0.0, "발행 간격을 계산할 글이 부족합니다"
    else:
        cons = 100 - _ratio_score(m["interval_cv"], 0.3, 1.5)
        basis = f"평균 발행 간격 {m['avg_interval_days']}일, 변동계수 {m['interval_cv']} (낮을수록 규칙적)"
    comps.append({"name": "꾸준함", "weight": 20, "score": cons, "basis": basis})
    last = m["last_post_days"]
    comps.append({"name": "최근성", "weight": 15,
                  "score": 0.0 if last is None else 100 - _ratio_score(last, 3, 30),
                  "basis": "글 없음" if last is None else f"마지막 발행 {last}일 전 (3일 이내 만점, 30일 이상 0점)"})
    exp = _exposure_score(exposure_rows) if exposure_rows else None
    if exp is not None:
        judged = [r for r in exposure_rows if r["status"] in ("top10", "found", "missing")]
        miss = sum(1 for r in judged if r["status"] == "missing")
        top = sum(1 for r in judged if r["status"] == "top10")
        comps.append({"name": "검색 노출", "weight": 35, "score": exp,
                      "basis": f"최근 글 {len(judged)}개 제목 검색: 10위 이내 {top}개, 100위 밖(누락 의심) {miss}개"})
    total = sum(c["score"] * c["weight"] for c in comps) / sum(c["weight"] for c in comps)
    for c in comps:
        c["score"] = round(c["score"], 1)
    return {"score": round(total, 1), **grade_of(total), "components": comps, "exposure_measured": exp is not None}


def blog_tips(m: dict, sc: dict, exposure_rows: list[dict] | None) -> list[str]:
    tips = []
    c = {x["name"]: x["score"] for x in sc["components"]}
    if c["활동량"] < 60:
        tips.append(f"최근 30일 발행이 {m['recent30']}개입니다. 주 3회 이상 꾸준히 발행하면 활동량 점수가 올라갑니다.")
    if c["꾸준함"] < 60:
        tips.append("발행 간격이 들쭉날쭉합니다. 몰아서 쓰기보다 요일·시간을 정해 일정하게 발행해 보세요.")
    if c["최근성"] < 60 and m["last_post_days"] is not None:
        tips.append(f"마지막 발행이 {m['last_post_days']}일 전입니다. 공백이 길어지면 검색 노출이 줄어들 수 있습니다.")
    if exposure_rows:
        miss = [r for r in exposure_rows if r["status"] == "missing"]
        if miss:
            tips.append(f"제목으로 검색해도 100위 안에 없는 글이 {len(miss)}개 있습니다(누락 의심). 중복 문서·과도한 키워드 반복·"
                        "광고성 문구·외부 링크를 점검하고, 같은 내용을 다시 올리는 것은 피하세요.")
    if not sc["exposure_measured"]:
        tips.append("설정에서 네이버 검색 API 키를 넣으면 '검색 노출' 항목까지 측정합니다.")
    if not tips:
        tips.append("지표가 고르게 좋습니다. 지금의 발행 리듬을 유지하세요.")
    return tips


def analyze_blog(rss: dict, api: OpenAPI | None, now: datetime | None = None, exposure_limit: int = 10) -> dict:
    now = now or now_kst()
    m = blog_metrics(rss, now)
    posts = sorted((p for p in rss["posts"] if p["date"]), key=lambda p: p["date"], reverse=True)
    rows = exposure_check(api, rss["blog_id"], posts, now, exposure_limit) if api and posts else None
    sc = score_blog(m, rows)
    return {
        "blog": {k: rss[k] for k in ("blog_id", "title", "description", "link", "image")},
        "metrics": m,
        **sc,
        "exposure": rows or [],
        "tips": blog_tips(m, sc, rows),
        "recent_posts": [{"title": p["title"], "link": p["link"], "date": _iso(p["date"]), "category": p["category"]}
                         for p in posts[:20]],
        "measured_at": _iso(now),
        "disclaimer": DISCLAIMER,
    }


# ================================================================ 키워드 분석
def saturation_grade(ratio: float | None, volume: int | None) -> dict:
    """포화도 = 누적 블로그 문서 수 / 월간 검색량 (블로거들이 흔히 쓰는 경험칙)."""
    if ratio is None:
        return {"grade": "-", "name": "판정 불가", "desc": "검색량 데이터가 없어 포화도를 계산할 수 없습니다."}
    for cut, g, name, desc in [(0.5, "S", "블루오션", "검색 수요에 비해 글이 매우 적습니다."),
                               (2, "A", "좋음", "경쟁 대비 수요가 충분합니다."),
                               (10, "B", "보통", "적당히 경쟁하는 키워드입니다."),
                               (50, "C", "경쟁 심함", "이미 글이 많습니다. 세부 키워드를 노려보세요.")]:
        if ratio < cut:
            break
    else:
        g, name, desc = "D", "레드오션", "글이 지나치게 많습니다. 롱테일(구체적인) 키워드를 추천합니다."
    if volume is not None and volume < 100:
        desc += " 다만 월 검색량이 100회 미만이라 유입 자체가 적을 수 있습니다."
    return {"grade": g, "name": name, "desc": desc}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def analyze_keyword(keyword: str, api: OpenAPI | None, ad: SearchAd | None, now: datetime | None = None) -> dict:
    now = now or now_kst()
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("키워드를 입력하세요.")
    rep: dict = {"keyword": keyword, "notes": [], "volume": None, "related": [], "top_posts": [], "trend": [],
                 "blog_total": None, "monthly_new_posts": None}
    if ad:
        rel = ad.keywords([keyword])
        main = next((k for k in rel if _norm(k["keyword"]) == _norm(keyword)), None)
        if main:
            rep["volume"] = {"pc": main["pc"], "mobile": main["mobile"], "total": main["total"], "comp": main["comp"]}
        rep["related"] = sorted((k for k in rel if k is not main), key=lambda k: -k["total"])[:40]
    else:
        rep["notes"].append("검색광고 API 키가 없어 월간 검색량·연관 키워드는 표시되지 않습니다(설정에서 추가).")

    if api:
        sim = api.search_blog(keyword, display=10, sort="sim")
        rep["blog_total"] = sim["total"]
        rep["top_posts"] = [{**it, "rank": i} for i, it in enumerate(sim["items"], 1)]
        recent = api.search_blog(keyword, display=100, sort="date")
        ds = sorted((datetime.strptime(it["date"], "%Y%m%d").replace(tzinfo=KST) for it in recent["items"]
                     if re.fullmatch(r"\d{8}", it["date"] or "")), reverse=True)
        if len(ds) >= 2:
            span = max((now - ds[-1]).total_seconds() / 86400, 1)
            rep["monthly_new_posts"] = round(len(ds) / span * 30) if len(ds) == 100 else sum(1 for d in ds if (now - d).days < 30)
        ages = [(now - datetime.strptime(it["date"], "%Y%m%d").replace(tzinfo=KST)).days
                for it in sim["items"] if re.fullmatch(r"\d{8}", it["date"] or "")]
        rep["top_avg_age_days"] = round(mean(ages)) if ages else None
        try:
            rep["trend"] = api.trend(keyword)
        except NaverError as e:
            rep["notes"].append(f"검색어 트렌드를 가져오지 못했습니다: {e}")
    else:
        rep["notes"].append("검색 API 키가 없어 발행량·상위 노출 글은 표시되지 않습니다(설정에서 추가).")

    vol = rep["volume"]["total"] if rep["volume"] else None
    ratio = (rep["blog_total"] / vol) if (vol and rep["blog_total"] is not None) else None
    rep["saturation"] = round(ratio, 2) if ratio is not None else None
    rep["grade"] = saturation_grade(ratio, vol)
    if rep.get("top_avg_age_days") is not None:
        rep["freshness"] = ("최신 글이 상위에 자주 올라오는 키워드입니다(새 글 진입 기회 있음)." if rep["top_avg_age_days"] <= 60
                            else "오래된 글이 상위를 지키고 있습니다(신규 진입이 어려울 수 있음).")
    rep["measured_at"] = _iso(now)
    return rep


# ================================================================ 순위 확인
def rank_check(api: OpenAPI, blog_id: str, keywords: list[str]) -> list[dict]:
    out = []
    for kw in [k.strip() for k in keywords if k.strip()][:20]:
        res = api.search_blog(kw, display=100, sort="sim")
        row = {"keyword": kw, "rank": None, "title": "", "link": "", "total": res["total"]}
        for i, it in enumerate(res["items"], 1):
            if _same_post(it["link"], blog_id, None):
                row.update(rank=i, title=it["title"], link=it["link"])
                break
        out.append(row)
    return out


# ================================================================ 포스팅 진단
AD_WORDS = ["최저가", "무료", "공짜", "100%", "대박", "클릭", "지금 바로", "할인", "특가", "당첨", "보장", "1등", "최고의"]
STOP = set("있습니다 합니다 그리고 하지만 그래서 정말 너무 있는 하는 것이 수도 있어요 했어요 입니다 이렇게 그런 이런 저는 제가 "
           "같아요 위해 때문에 또한 이번 오늘 그냥 많이 바로 하고 해서 있고 없는 됩니다 하면 좋은".split())


def _status(ok: bool, warn: bool) -> str:
    return "good" if ok else ("warn" if warn else "bad")


def diagnose_post(title: str, text: str, keyword: str = "", images: int | None = None) -> dict:
    title, text, keyword = (title or "").strip(), (text or "").strip(), (keyword or "").strip()
    if not text:
        raise ValueError("본문이 비어 있습니다.")
    chars = len(re.sub(r"\s", "", text))
    paras = [p for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    sents = [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n", text) if len(s.strip()) > 1]
    kw = _norm(keyword)
    body_n = _norm(text)
    kw_count = body_n.count(kw) if kw else 0
    density = (kw_count * len(kw) / max(len(body_n), 1)) if kw else 0
    words = Counter(w for w in re.findall(r"[가-힣A-Za-z]{2,}", text) if w not in STOP)
    checks = []

    def add(name, status, msg, weight=10):
        checks.append({"name": name, "status": status, "msg": msg, "weight": weight})

    tl = len(title)
    add("제목 길이", _status(15 <= tl <= 40, 8 <= tl <= 55), f"{tl}자 (15~40자 권장)")
    if kw:
        pos = _norm(title).find(kw)
        add("제목 키워드", _status(0 <= pos <= 10, pos >= 0),
            "제목에 키워드가 없습니다." if pos < 0 else f"제목 {'앞부분' if pos <= 10 else '뒷부분'}에 포함", 15)
        add("본문 키워드 횟수", _status(3 <= kw_count <= 15 and density <= 0.05, 1 <= kw_count <= 25 and density <= 0.08),
            f"{kw_count}회, 밀도 {density:.1%} (3~15회·5% 이하 권장, 과하면 스팸으로 볼 수 있음)", 15)
        first = _norm(text[:300])
        add("도입부 키워드", _status(kw in first, False), "첫 300자 안에 키워드가 " + ("있습니다" if kw in first else "없습니다"), 5)
    add("본문 분량", _status(chars >= 1500, chars >= 800), f"공백 제외 {chars:,}자 (1,500자 이상 권장)", 15)
    if images is None:
        add("이미지", "info", "붙여넣기 진단은 이미지 수를 알 수 없습니다. 글 주소로 진단하면 확인합니다.", 0)
    else:
        add("이미지", _status(images >= 5, images >= 1), f"{images}장 (직접 찍은 사진 5장 이상 권장)", 10)
    avg_para = mean(len(p) for p in paras) if paras else 0
    add("문단 길이", _status(avg_para <= 150, avg_para <= 300), f"문단 평균 {avg_para:.0f}자 (모바일 가독성: 150자 이하 권장)", 5)
    long_s = [s for s in sents if len(s) > 120]
    add("긴 문장", _status(not long_s, len(long_s) <= 3), f"120자 넘는 문장 {len(long_s)}개", 5)
    dup = [s for s, n in Counter(s for s in sents if len(s) >= 15).items() if n > 1]
    add("중복 문장", _status(not dup, False), f"같은 문장 반복 {len(dup)}개" + (f": “{dup[0][:30]}…”" if dup else ""), 10)
    over = [(w, n) for w, n in words.most_common(10) if n >= max(12, chars // 150) and _norm(w) != kw]
    add("반복 단어", _status(not over, len(over) <= 1), ("과하게 반복된 단어: " + ", ".join(f"{w}({n})" for w, n in over[:5]))
        if over else "과도한 반복 없음", 5)
    ads = [w for w in AD_WORDS if w in text or w in title]
    add("광고성 표현", _status(not ads, len(ads) <= 2), ("발견: " + ", ".join(ads)) if ads else "없음", 5)
    contacts = re.findall(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}|카톡\s*ID|오픈채팅", text)
    add("연락처 노출", _status(not contacts, False), f"{len(contacts)}건" + (" (전화번호/카톡 유도는 스팸 신호)" if contacts else ""), 5)

    scored = [c for c in checks if c["weight"] > 0]
    pts = {"good": 1.0, "warn": 0.5, "bad": 0.0}
    score = sum(pts[c["status"]] * c["weight"] for c in scored) / sum(c["weight"] for c in scored) * 100
    return {"score": round(score, 1), **grade_of(score), "checks": checks,
            "stats": {"chars": chars, "chars_with_spaces": len(text), "paragraphs": len(paras), "sentences": len(sents),
                      "images": images, "keyword_count": kw_count,
                      "top_words": [{"label": w, "value": n} for w, n in words.most_common(10)]},
            "disclaimer": "자체 기준의 점검 결과입니다. 네이버의 실제 평가 방식과 다를 수 있습니다."}
