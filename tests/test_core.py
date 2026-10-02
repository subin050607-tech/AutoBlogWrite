import base64
import hashlib
import hmac

import pytest

from blogscope import analysis, naver
from fakes import NOW, POST_HTML, FakeAd, FakeAPI, rss_xml


@pytest.mark.parametrize("s,exp", [
    ("kth7405", ("kth7405", None)),
    ("https://blog.naver.com/KTH7405", ("kth7405", None)),
    ("blog.naver.com/kth7405/224410368545", ("kth7405", "224410368545")),
    ("https://m.blog.naver.com/kth7405/224410368545?referrerCode=0", ("kth7405", "224410368545")),
    ("https://blog.naver.com/PostView.naver?blogId=abc_1&logNo=223", ("abc_1", "223")),
])
def test_parse_blog_ref(s, exp):
    assert naver.parse_blog_ref(s) == exp


@pytest.mark.parametrize("bad", ["", "https://evil.com/kth7405", "https://blog.naver.com/", "아이디", "a"])
def test_parse_blog_ref_rejects(bad):
    with pytest.raises(ValueError):
        naver.parse_blog_ref(bad)


def test_parse_rss():
    r = naver.parse_rss(rss_xml(n=3), "tester")
    assert r["title"] == "테스트 블로그" and len(r["posts"]) == 3
    p = r["posts"][0]
    assert p["log_no"] == "223000000000" and p["link"] == "https://blog.naver.com/tester/223000000000"
    assert p["summary"] == "요약 0 & 내용" and p["tags"] == ["캠핑", "준비물"] and p["date"].tzinfo
    with pytest.raises(naver.NaverError):
        naver.parse_rss(b"<html>not rss", "x")


def test_parse_post_html_smarteditor():
    p = naver.parse_post_html(POST_HTML)
    assert p["title"] == "캠핑 준비물 총정리"
    assert p["text"] == "캠핑 준비물을 정리했어요.\n텐트와 의자가 필요합니다."
    assert p["images"] == 2  # 푸터 이미지는 제외
    with pytest.raises(naver.NaverError):
        naver.parse_post_html("<html>no body</html>")


def test_searchad_signature_and_counts():
    ad = naver.SearchAd("key", "secret", "123")
    expected = base64.b64encode(hmac.new(b"secret", b"1700.GET./keywordstool", hashlib.sha256).digest()).decode()
    assert ad.sign("1700", "GET", "/keywordstool") == expected
    assert naver.parse_count("< 10") == 5 and naver.parse_count(1234) == 1234 and naver.parse_count("1,200") == 1200


def test_blog_analysis_active_blog_with_exposure():
    rss = naver.parse_rss(rss_xml(n=20, every_days=2), "tester")
    rep = analysis.analyze_blog(rss, FakeAPI(), now=NOW)
    m = rep["metrics"]
    assert m["recent30"] == 15 and m["avg_interval_days"] == 2.0 and m["interval_cv"] == 0.0
    comps = {c["name"]: c["score"] for c in rep["components"]}
    assert comps["활동량"] == 100 and comps["꾸준함"] == 100 and comps["최근성"] == 100
    # 10개 검사: 짝수 5개 1위, 홀수 5개 누락 → 50점
    assert comps["검색 노출"] == 50.0
    assert sum(1 for e in rep["exposure"] if e["status"] == "missing") == 5
    assert rep["exposure_measured"] and any("누락" in t for t in rep["tips"])
    assert rep["grade"] in "SABCD" and "공식" in rep["disclaimer"]


def test_blog_analysis_without_api_and_inactive():
    rss = naver.parse_rss(rss_xml(n=5, every_days=20, start_days=40), "tester")
    rep = analysis.analyze_blog(rss, None, now=NOW)
    names = [c["name"] for c in rep["components"]]
    assert "검색 노출" not in names and not rep["exposure_measured"]
    assert rep["score"] < 40 and rep["grade"] == "D"
    assert any("API 키" in t for t in rep["tips"])


def test_exposure_skips_fresh_posts():
    rss = naver.parse_rss(rss_xml(n=3, start_days=0.1), "tester")
    rows = analysis.exposure_check(FakeAPI(), "tester", rss["posts"], NOW)
    assert rows[0]["status"] == "pending" and rows[0]["rank"] is None


def test_keyword_analysis_full():
    rep = analysis.analyze_keyword("캠핑 준비물", FakeAPI(), FakeAd(), now=NOW)
    assert rep["volume"]["total"] == 10000 and rep["blog_total"] == 5000
    assert rep["saturation"] == 0.5 and rep["grade"]["name"] == "좋음"
    assert [k["keyword"] for k in rep["related"]] == ["캠핑의자", "백패킹"]
    assert rep["top_posts"][1]["rank"] == 2 and len(rep["trend"]) == 10
    assert rep["monthly_new_posts"] == 6 and rep["freshness"]


def test_keyword_analysis_partial_keys():
    only_ad = analysis.analyze_keyword("캠핑준비물", None, FakeAd(), now=NOW)
    assert only_ad["volume"] and only_ad["blog_total"] is None and only_ad["grade"]["grade"] == "-"
    assert any("검색 API" in n for n in only_ad["notes"])
    only_api = analysis.analyze_keyword("캠핑", FakeAPI(), None, now=NOW)
    assert only_api["volume"] is None and only_api["blog_total"] == 5000
    with pytest.raises(ValueError):
        analysis.analyze_keyword("  ", FakeAPI(), None)


@pytest.mark.parametrize("ratio,g", [(0.1, "S"), (1, "A"), (5, "B"), (20, "C"), (100, "D")])
def test_saturation_grades(ratio, g):
    assert analysis.saturation_grade(ratio, 1000)["grade"] == g
    assert "100회 미만" in analysis.saturation_grade(ratio, 50)["desc"]


def test_rank_check():
    res = analysis.rank_check(FakeAPI(), "tester", ["캠핑", " ", "텐트"])
    assert [r["rank"] for r in res] == [2, 2] and res[0]["link"].endswith("/223000000001")
    res = analysis.rank_check(FakeAPI(blog_id="someone"), "tester", ["캠핑"])
    assert res[0]["rank"] is None


def test_diagnose_post_good_and_bad():
    good_text = "캠핑 준비물을 고를 때는 계절과 인원을 먼저 생각해야 합니다.\n\n" + "\n\n".join(
        f"{i}번째 장비는 무게가 {i * 3 + 1}킬로그램 정도이고 설치에 {i + 2}분이 걸려요. "
        f"직접 써보니 {i}번 제품은 바람이 불 때 특히 안정적이었습니다." for i in range(30)) + "\n\n캠핑 준비물 정리였습니다. 캠핑 준비물 목록을 저장해 두세요."
    good = analysis.diagnose_post("캠핑 준비물 체크리스트 초보 가이드", good_text, "캠핑 준비물", images=6)
    s = {c["name"]: c["status"] for c in good["checks"]}
    assert s["제목 키워드"] == "good" and s["본문 분량"] == "good" and s["이미지"] == "good"
    assert s["중복 문장"] == "good"

    bad = analysis.diagnose_post("후기", "최저가 대박 할인! 연락 010-1234-5678\n" + "같은 문장을 반복합니다 정말로요.\n" * 3,
                                 "캠핑 준비물", images=0)
    s = {c["name"]: c["status"] for c in bad["checks"]}
    assert s["제목 키워드"] == "bad" and s["광고성 표현"] == "bad" and s["연락처 노출"] == "bad"
    assert s["중복 문장"] == "bad" and s["이미지"] == "bad"
    assert bad["score"] < good["score"]
    unknown = analysis.diagnose_post("t", "본문", "", images=None)
    assert any(c["status"] == "info" for c in unknown["checks"])
    with pytest.raises(ValueError):
        analysis.diagnose_post("t", "   ")


def test_keyword_spam_detected():
    text = "캠핑 준비물 " * 80
    s = {c["name"]: c["status"] for c in analysis.diagnose_post("캠핑 준비물", text, "캠핑 준비물")["checks"]}
    assert s["본문 키워드 횟수"] == "bad"
