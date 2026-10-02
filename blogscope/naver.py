"""네이버 데이터 수집.

- 공식 무료 API: 검색(블로그) · 데이터랩(검색어 트렌드) — developers.naver.com
- 공식 무료 API: 검색광고 키워드도구(월간 검색량·연관 키워드) — searchad.naver.com
- 공개 페이지: 블로그 RSS, 글 본문(PostView)

API 키 없이도 RSS 기반 블로그 분석은 동작한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


class NaverError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _explain(code: int, body: str) -> str:
    hint = {
        401: "API 인증 실패 — 설정의 키 값을 확인하세요.",
        403: "API 권한 없음 — 네이버 개발자센터 애플리케이션에 해당 API(검색/데이터랩)를 추가했는지 확인하세요.",
        404: "찾을 수 없습니다 — 아이디/주소가 맞는지, 공개 블로그인지 확인하세요.",
        429: "호출 한도 초과 — 잠시 후 다시 시도하세요.",
    }.get(code, "요청 실패")
    return f"{hint} (HTTP {code}) {body[:200]}".strip()


def request(url: str, headers: dict | None = None, data: bytes | None = None, method: str | None = None,
            timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, data=data, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if (e.code == 429 or e.code >= 500) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise NaverError(_explain(e.code, body), e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < 2:
                time.sleep(1)
                continue
            raise NaverError(f"네트워크 연결 실패: {e}") from None
    raise NaverError("요청 실패")


def clean(s: str | None) -> str:
    """HTML 태그 제거 + 엔티티 해제 + 공백 정리."""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s).replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- 블로그 주소 해석
def parse_blog_ref(s: str) -> tuple[str, str | None]:
    """아이디 / blog.naver.com/아이디 / .../아이디/글번호 / PostView?blogId=..&logNo=.. → (아이디, 글번호)"""
    s = (s or "").strip()
    if not s:
        raise ValueError("블로그 아이디나 주소를 입력하세요.")
    log_no = None
    if re.fullmatch(r"[A-Za-z0-9_-]+", s):
        blog_id = s
    else:
        u = urllib.parse.urlparse(s if "://" in s else "https://" + s)
        host = (u.hostname or "").lower()
        if not (host == "blog.naver.com" or host.endswith(".blog.naver.com")):
            raise ValueError("네이버 블로그 주소(blog.naver.com)가 아닙니다.")
        q = urllib.parse.parse_qs(u.query)
        if "blogId" in q:
            blog_id = q["blogId"][0]
            log_no = (q.get("logNo") or [None])[0]
        else:
            parts = [p for p in u.path.split("/") if p]
            if not parts:
                raise ValueError("주소에 블로그 아이디가 없습니다.")
            blog_id = parts[0]
            if len(parts) > 1 and parts[1].isdigit():
                log_no = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", blog_id):
        raise ValueError(f"블로그 아이디 형식이 아닙니다: {blog_id}")
    if log_no is not None and not log_no.isdigit():
        log_no = None
    return blog_id.lower(), log_no


# ---------------------------------------------------------------- RSS
def _parse_date(s: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(s) if s else None
    except (TypeError, ValueError):
        return None


def parse_rss(raw: bytes, blog_id: str) -> dict:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise NaverError("RSS를 읽을 수 없습니다. 없는 블로그이거나 비공개/RSS 차단 상태일 수 있습니다.", 404) from None
    ch = root.find("channel")
    if ch is None:
        raise NaverError("RSS 형식이 아닙니다.", 404)
    posts = []
    for it in ch.findall("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/(\d{6,})", link)
        posts.append({
            "title": clean(it.findtext("title")),
            "link": link.split("?")[0],
            "log_no": m.group(1) if m else None,
            "date": _parse_date(it.findtext("pubDate")),
            "category": clean(it.findtext("category")),
            "summary": clean(it.findtext("description"))[:300],
            "tags": [t.strip() for t in (it.findtext("tag") or "").split(",") if t.strip()],
        })
    return {
        "blog_id": blog_id,
        "title": clean(ch.findtext("title")),
        "description": clean(ch.findtext("description")),
        "link": f"https://blog.naver.com/{blog_id}",
        "image": clean(ch.findtext("image/url")),
        "posts": posts,
    }


def fetch_rss(blog_id: str) -> dict:
    return parse_rss(request(f"https://rss.blog.naver.com/{urllib.parse.quote(blog_id)}.xml"), blog_id)


# ---------------------------------------------------------------- 글 본문 (스마트에디터 ONE / 구 에디터)
_END_MARKERS = ("se-viewer-footer", "post_footer_contents", "wrap_postcomment", "post-btn", "btn_post")


def parse_post_html(page: str) -> dict:
    title = ""
    m = re.search(r'class="[^"]*se-title-text[^"]*"[^>]*>(.*?)</div>', page, re.S)
    if m:
        title = clean(m.group(1))
    if not title:
        m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', page)
        title = clean(m.group(1)) if m else ""

    start = page.find("se-main-container")
    old = start < 0
    if old:
        start = page.find('id="postViewArea"')
    if start < 0:
        raise NaverError("본문을 찾지 못했습니다. 비공개 글이거나 페이지 구조가 바뀌었을 수 있습니다. 본문을 직접 붙여넣어 진단하세요.")
    ends = [i for i in (page.find(mk, start) for mk in _END_MARKERS) if i > 0]
    seg = page[start:min(ends) if ends else start + 800_000]

    if old:
        text = "\n".join(x for x in (clean(p) for p in re.split(r"</p>|<br\s*/?>|</div>", seg)) if x)
        images = len(re.findall(r"<img\b", seg))
    else:
        paras = re.findall(r'<p[^>]*class="[^"]*se-text-paragraph[^"]*"[^>]*>(.*?)</p>', seg, re.S)
        text = "\n".join(x for x in (clean(p) for p in paras) if x)
        images = len(re.findall(r'class="[^"]*se-image-resource', seg))
    links = len(re.findall(r'class="[^"]*se-oglink|<a\s[^>]*href="https?://', seg))
    return {"title": title, "text": text, "images": images, "links": links}


def fetch_post(blog_id: str, log_no: str) -> dict:
    qs = urllib.parse.urlencode({"blogId": blog_id, "logNo": log_no, "redirect": "Dlog", "widgetTypeCall": "true"})
    page = request(f"https://blog.naver.com/PostView.naver?{qs}", {"Referer": f"https://blog.naver.com/{blog_id}"})
    return parse_post_html(page.decode("utf-8", "replace"))


# ---------------------------------------------------------------- 검색 / 데이터랩 API (무료, 하루 25,000 / 1,000회)
class OpenAPI:
    BASE = "https://openapi.naver.com"

    def __init__(self, client_id: str, client_secret: str, base: str | None = None):
        self.cid, self.secret, self.base = client_id, client_secret, (base or self.BASE).rstrip("/")

    def _h(self) -> dict:
        return {"X-Naver-Client-Id": self.cid, "X-Naver-Client-Secret": self.secret}

    def search_blog(self, query: str, display: int = 100, start: int = 1, sort: str = "sim") -> dict:
        qs = urllib.parse.urlencode({"query": query, "display": display, "start": start, "sort": sort})
        d = json.loads(request(f"{self.base}/v1/search/blog.json?{qs}", self._h()))
        items = [{
            "title": clean(x.get("title")),
            "link": x.get("link", ""),
            "description": clean(x.get("description")),
            "blogger": clean(x.get("bloggername")),
            "blogger_link": x.get("bloggerlink", ""),
            "date": x.get("postdate", ""),
        } for x in d.get("items", [])]
        return {"total": int(d.get("total", 0)), "items": items}

    def trend(self, keyword: str, months: int = 12) -> list[dict]:
        end = date.today()
        start = end - timedelta(days=months * 31)
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "month",
                "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}]}
        d = json.loads(request(f"{self.base}/v1/datalab/search", {**self._h(), "Content-Type": "application/json"},
                               json.dumps(body).encode("utf-8"), "POST"))
        res = d.get("results") or [{}]
        return [{"period": x["period"], "ratio": x["ratio"]} for x in res[0].get("data", [])]


# ---------------------------------------------------------------- 검색광고 키워드도구 (무료, 광고비 집행 불필요)
def parse_count(v) -> int:
    """'< 10' 같은 값은 5 로 근사."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "")
    if "<" in s:
        return 5
    return int(re.sub(r"\D", "", s) or 0)


class SearchAd:
    BASE = "https://api.searchad.naver.com"

    def __init__(self, api_key: str, secret: str, customer_id: str, base: str | None = None):
        self.api_key, self.secret, self.customer = api_key, secret, str(customer_id)
        self.base = (base or self.BASE).rstrip("/")

    def sign(self, ts: str, method: str, uri: str) -> str:
        mac = hmac.new(self.secret.encode(), f"{ts}.{method}.{uri}".encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def keywords(self, hints: list[str]) -> list[dict]:
        uri = "/keywordstool"
        ts = str(int(time.time() * 1000))
        qs = urllib.parse.urlencode({"hintKeywords": ",".join(h.replace(" ", "") for h in hints[:5]), "showDetail": "1"})
        headers = {"X-Timestamp": ts, "X-API-KEY": self.api_key, "X-Customer": self.customer,
                   "X-Signature": self.sign(ts, "GET", uri)}
        d = json.loads(request(f"{self.base}{uri}?{qs}", headers))
        out = []
        for k in d.get("keywordList", []):
            pc, mo = parse_count(k.get("monthlyPcQcCnt")), parse_count(k.get("monthlyMobileQcCnt"))
            out.append({"keyword": k.get("relKeyword", ""), "pc": pc, "mobile": mo, "total": pc + mo,
                        "comp": k.get("compIdx", "")})
        return out
