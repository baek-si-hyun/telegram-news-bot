"""기사 본문 수집 — 판정이 애매한 기사에 한해서만 쓴다.

## 왜 필요한가

네이버 검색 API 는 제목과 **121자짜리 스니펫**만 준다. 그것만으로 판정하면 '부동산 정책
기사'와 '부동산을 소재로 쓴 정치 기사'가 잘 안 갈린다. 실측(라벨 748건)으로:

    제목 + 스니펫            AUC 0.807
    제목 + 스니펫 + 본문 1500자  AUC 0.850   (+0.043)

라벨을 더 모으거나(+0.005) 수작업 특징을 넣는 것(-0.006)보다 훨씬 크다.

## 왜 전부 받지 않는가

한 주기에 키워드 40개 × 결과 30건 = 1,200건이 후보다. 전부 받으면 주기를 넘긴다.
그래서 **중복 제거를 통과한 신규 기사 중 스니펫 확률이 애매한 구간**만 받는다.
로그상 주기당 신규는 보통 수 건~수십 건이라, 이 방식이면 주기당 요청이 그 정도로 그친다.

실패해도 흐름을 막지 않는다 — 본문을 못 받으면 스니펫 판정을 그대로 쓴다.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TIMEOUT = 8
MAX_WORKERS = 8
MAX_BODY_CHARS = 2000

# 브라우저로 위장하지 않고 봇임을 밝힌다. 이 UA 를 막는 사이트는 본문 없이 스니펫으로 판정한다
# (막힌다고 헤더를 바꿔 우회하지 않는다).
_HEADERS = {
    "User-Agent": "telegram-news-bot/1.0 (+https://github.com/baek-si-hyun/telegram-news-bot)",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 흔한 본문 컨테이너 (네이버 뉴스 + 국내 언론사 CMS 다수)
# 언론사 전용 셀렉터는 범용(article 등)보다 앞에 둔다 — 범용이 먼저 걸리면 짧은 조각만 잡힌다.
# 2026-11-19 보강: 정직 UA 로 200 을 받고도 본문을 못 뽑던 언론사들
#   뉴스1 #articleBodyContent(s 없음) · 노컷 #pnlContent · YTN #CmAdContent ·
#   동아 section.news_view · 스포츠동아 #article_body(id — .article_body 클래스와 다르다)
_SELECTORS = [
    "#dic_area", "#articleBodyContents", "#articleBodyContent", "#newsct_article",
    "#article-view-content-div", "#pnlContent", "#CmAdContent", "section.news_view", "#article_body",
    "#articleBody", ".article-body", ".news_body", ".article_body", "article",
]
_CONTAINER_CSS = ", ".join(_SELECTORS)

_BOILERPLATE = re.compile(r"(무단전재|재배포\s*금지|저작권자|ⓒ|Copyright|기자\s*=|사진=).{0,80}")


def _ld_article_body(soup: BeautifulSoup) -> str:
    """구조화 데이터(JSON-LD)의 articleBody. 본문을 스크립트로 그리는 페이지의 마지막 수단.

    description(요약)은 쓰지 않는다 — 스니펫과 같은 길이라 본문 모델에 넣을 이유가 없다.
    """
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except ValueError:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            body = obj.get("articleBody") if isinstance(obj, dict) else None
            if isinstance(body, str) and len(body) > 200:
                return body
    return ""


def _next_data_body(soup: BeautifulSoup) -> str:
    """Next.js 페이지의 __NEXT_DATA__ 에 든 본문 문단. 뉴스1 은 본문 <p> 를 비워 두고 여기서 그린다
    (props.pageProps.articleView.contentArrange[].content — 문단마다 HTML 조각)."""
    s = soup.find("script", id="__NEXT_DATA__")
    if not s:
        return ""
    try:
        view = json.loads(s.string or "")["props"]["pageProps"]["articleView"]
        blocks = view["contentArrange"]
    except (ValueError, KeyError, TypeError):
        return ""
    if not isinstance(blocks, list):
        return ""
    parts = []
    for b in blocks:
        # 사진 블록(src 가 있는 것)의 content 는 캡션이라 뺀다
        if isinstance(b, dict) and not b.get("src") and isinstance(b.get("content"), str):
            parts.append(BeautifulSoup(b["content"], "html.parser").get_text(" ", strip=True))
    text = " ".join(p for p in parts if p)
    return text if len(text) > 200 else ""


def _extract(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # script 를 지우기 전에 읽는다
    ld_body = _ld_article_body(soup) or _next_data_body(soup)
    for bad in soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
        bad.decompose()
    # form 은 본문 컨테이너를 품지 않은 것만 지운다 — ASP.NET 언론사(노컷 등)는 페이지 전체가
    # <form> 안이라 통째로 지우면 본문까지 사라진다. 나머지 form(좋아요·댓글 입력란)은 지운다.
    for f in soup("form"):
        if not f.select_one(_CONTAINER_CSS):
            f.decompose()
    for sel in _SELECTORS:
        node = soup.select_one(sel)
        if node:
            txt = node.get_text(" ", strip=True)
            # 길이는 상투어(ⓒ·무단전재 …)를 걷어낸 뒤로 잰다 — 사진 캡션·저작권 줄만 든 껍데기
            # 컨테이너(뉴스1 의 <article>)가 200자를 넘겨 진짜 본문 경로를 가로막았다.
            if len(_BOILERPLATE.sub("", txt)) > 200:
                return txt
    for bad in soup("form"):   # 남은 form(본문 컨테이너를 품었지만 본문을 못 찾은 경우)도 <p> 폴백에선 뺀다
        bad.decompose()
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    p_text = " ".join(p for p in paras if len(p) > 20)
    # 구조화 데이터 본문과 <p> 모음 중 긴 쪽 — 데일리안처럼 articleBody 를 잘라 싣는 곳은 <p> 가 더 길다.
    return ld_body if len(ld_body) > len(_BOILERPLATE.sub("", p_text)) else p_text


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    text = _BOILERPLATE.sub("", text)
    return text.strip()[:MAX_BODY_CHARS]


def _fetch_one(url: str) -> str:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        resp.encoding = resp.apparent_encoding or resp.encoding
        return _clean(_extract(resp.text))
    except Exception as e:  # noqa: BLE001 — 본문 실패는 절대 흐름을 막지 않는다
        logger.debug(f"[Body] 수집 실패 {url}: {type(e).__name__}")
        return ""


def fetch_many(urls: list[str]) -> dict[str, str]:
    """URL 목록의 본문을 병렬로 받는다. 실패한 건은 빈 문자열."""
    urls = [u for u in urls if u]
    if not urls:
        return {}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(urls))) as ex:
        for url, body in zip(urls, ex.map(_fetch_one, urls)):
            out[url] = body
    ok = sum(1 for b in out.values() if len(b) > 200)
    logger.info(f"[Body] 본문 수집 {ok}/{len(urls)}건 성공")
    return out
