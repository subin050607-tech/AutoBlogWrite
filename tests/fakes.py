"""테스트용 가짜 네이버 데이터."""
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=KST)


def rss_xml(blog_id="tester", n=12, every_days=2.0, start_days=1.0, title="테스트 블로그"):
    items = []
    for i in range(n):
        d = NOW - timedelta(days=start_days + i * every_days)
        items.append(f"""<item><title><![CDATA[글 제목 {i} 캠핑 준비물]]></title>
<link>https://blog.naver.com/{blog_id}/22300000{i:04d}?fromRss=true&amp;trackingCode=rss</link>
<description><![CDATA[<p>요약 {i} &amp; 내용</p>]]></description><category>캠핑</category><tag>캠핑,준비물</tag>
<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S +0900')}</pubDate></item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>
<link>https://blog.naver.com/{blog_id}</link><description>설명</description>{''.join(items)}</channel></rss>""".encode()


class FakeAPI:
    """제목 검색 시 짝수 글은 1위, 홀수 글은 누락. 키워드 검색은 2위에 내 글."""

    def __init__(self, blog_id="tester"):
        self.blog_id, self.calls = blog_id, []

    def search_blog(self, query, display=100, start=1, sort="sim"):
        self.calls.append((query, display, sort))
        other = {"title": "남의 글", "link": "https://blog.naver.com/other/1111111", "description": "", "blogger": "남",
                 "blogger_link": "", "date": "20260901"}
        if query.startswith("글 제목"):
            i = int(query.split()[2])
            items = [{**other, "link": f"https://blog.naver.com/{self.blog_id}/22300000{i:04d}"}] if i % 2 == 0 else [other]
            return {"total": 10, "items": items}
        if sort == "date":
            items = [{**other, "date": (NOW - timedelta(days=d)).strftime("%Y%m%d")} for d in range(0, 50, 5)]
            return {"total": 5000, "items": items}
        mine = {**other, "title": "내 글", "link": f"https://blog.naver.com/{self.blog_id}/223000000001", "date": "20260920"}
        return {"total": 5000, "items": [other, mine]}

    def trend(self, keyword, months=12):
        return [{"period": f"2026-{m:02d}-01", "ratio": m * 8.0} for m in range(1, 11)]


class FakeAd:
    def keywords(self, hints):
        return [{"keyword": "캠핑준비물", "pc": 1000, "mobile": 9000, "total": 10000, "comp": "높음"},
                {"keyword": "캠핑의자", "pc": 300, "mobile": 2000, "total": 2300, "comp": "중간"},
                {"keyword": "백패킹", "pc": 5, "mobile": 40, "total": 45, "comp": "낮음"}]


POST_HTML = """<html><head><meta property="og:title" content="OG 제목"></head><body>
<div class="se-module se-title-text"><span>캠핑 준비물 총정리</span></div>
<div class="se-main-container">
<p class="se-text-paragraph"><span>캠핑 준비물을 정리했어요.</span></p>
<p class="se-text-paragraph"><span>&#x200b;</span></p>
<img class="se-image-resource" src="a.jpg"><img class="se-image-resource" src="b.jpg">
<p class="se-text-paragraph"><span>텐트와 의자가 필요합니다.</span></p>
</div><div class="se-viewer-footer"><img class="se-image-resource" src="footer.jpg"></div></body></html>"""


class FakeLLM:
    """프롬프트 내용으로 기획/본문/재작성 응답을 구분해 돌려주는 가짜 AI."""

    def __init__(self):
        self.prompts = []

    def complete(self, system, user, json_mode=True, temperature=0.8):
        import json
        self.prompts.append(user)
        if "다시 쓸 부분" in user:
            return json.dumps({"heading": "새 소제목", "content": "다시 쓴 내용입니다.\n\n- 항목"}, ensure_ascii=False)
        if "기획하세요" in user:
            return "```json\n" + json.dumps({"titles": ["캠핑 준비물 체크리스트 총정리", "캠핑 준비물 B", "C", "D", "E", "F"],
                                             "outline": [{"heading": "텐트 고르기", "points": ["크기"]}, {"heading": "의자와 테이블"},
                                                         {"heading": ""}],
                                             "hashtags": ["#캠핑", "캠핑 준비물", "캠핑"]}, ensure_ascii=False) + "\n```"
        para = "캠핑 준비물을 챙길 때는 인원과 계절을 먼저 생각해요. 무게와 부피도 꼭 확인하세요. "
        return json.dumps({
            "title": "캠핑 준비물 체크리스트 총정리",
            "intro": "처음 캠핑을 가면 무엇을 챙길지 막막하죠.\n\n" + para * 3,
            "sections": [{"heading": h, "content": (para + f"{h} 이야기를 해볼게요. ") * 4 + "\n\n[사진: " + h + " 사진]\n\n- **핵심** 하나\n- 핵심 둘"}
                         for h in ("텐트 고르기", "의자와 테이블", "조명과 전기", "주방 도구", "침낭과 매트")],
            "outro": "캠핑 준비물 정리였습니다. [확인 필요: 캠핑장 규정]",
            "faq": [{"q": "텐트는 몇 인용?", "a": "인원 +1 추천"}],
            "hashtags": ["캠핑", "캠핑준비물"]}, ensure_ascii=False)
