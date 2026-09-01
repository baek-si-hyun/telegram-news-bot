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

import logging
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TIMEOUT = 8
MAX_WORKERS = 8
MAX_BODY_CHARS = 2000

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 흔한 본문 컨테이너 (네이버 뉴스 + 국내 언론사 CMS 다수)
_SELECTORS = [
    "#dic_area", "#articleBodyContents", "#newsct_article", "#article-view-content-div",
    "#articleBody", ".article-body", ".news_body", ".article_body", "article",
]

_BOILERPLATE = re.compile(r"(무단전재|재배포\s*금지|저작권자|ⓒ|Copyright|기자\s*=|사진=).{0,80}")


def _extract(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
        bad.decompose()
    for sel in _SELECTORS:
        node = soup.select_one(sel)
        if node:
            txt = node.get_text(" ", strip=True)
            if len(txt) > 200:
                return txt
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    return " ".join(p for p in paras if len(p) > 20)


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
