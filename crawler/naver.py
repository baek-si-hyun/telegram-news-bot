import time
import logging
import re
import requests
from datetime import datetime
from config import (
    NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NEWS_DISPLAY_COUNT, REQUEST_DELAY_SECONDS,
)
from storage import history
from ml import classifier

logger = logging.getLogger(__name__)

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news"


def _relevance_score(title: str, description: str,
                     positive_weights: dict, negative_weights: dict) -> int:
    """제목+설명의 도메인 관련성 점수 계산 (양수=관련, 음수=무관)

    제목에 있는 단어는 2배 가중치 (제목이 기사 핵심을 반영하므로)
    """
    score = 0
    for word, weight in positive_weights.items():
        if word in title:
            score += weight * 2
        elif word in description:
            score += weight
    for word, weight in negative_weights.items():
        if word in title:
            score += weight * 2
        elif word in description:
            score += weight
    return score


def _clean_html(text: str) -> str:
    """HTML 태그 및 특수문자 제거"""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'")
    return text.strip()


def _fetch(query: str, retry: int = 3) -> list[dict]:
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": query,
        "display": NEWS_DISPLAY_COUNT,
        "start": 1,
        "sort": "date",
    }

    for attempt in range(1, retry + 1):
        try:
            response = requests.get(NAVER_NEWS_URL, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                try:
                    return response.json().get("items", [])
                except (ValueError, KeyError):
                    logger.error(f"[Naver] JSON 파싱 실패 ({query}): {response.text[:200]}")
                    return []
            elif response.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"[Naver] Rate limit 초과 ({query}), {wait}초 후 재시도")
                time.sleep(wait)
            else:
                logger.error(f"[Naver] 오류 {response.status_code}: {response.text}")
                return []
        except requests.RequestException as e:
            logger.error(f"[Naver] 요청 실패 ({query}) 시도 {attempt}/{retry}: {e}")
            if attempt < retry:
                time.sleep(2 ** attempt)

    logger.warning(f"[Naver] 재시도 모두 소진 ({query})")
    return []


def search(keyword: str, query: str, profile: dict, topic_key: str) -> list[dict]:
    """
    네이버 뉴스 검색 후 (토픽 프로필 기준) 정제된 뉴스 목록 반환

    profile: config.TOPICS[topic_key] (positive_weights/negative_weights/threshold/strict_match)
    반환 형식:
    [{"source": "naver", "keyword": ..., "title": ..., "url": ...}, ...]
    """
    positive_weights = profile["positive_weights"]
    negative_weights = profile["negative_weights"]
    threshold = profile["threshold"]
    strict_match = profile.get("strict_match", set())
    require_title = profile.get("require_title_keyword", False)
    require_any = profile.get("require_any_terms")  # 핵심어 게이트(옵션)

    raw_items = _fetch(query)
    results = []
    history_records = []
    now = datetime.now().isoformat()

    for item in raw_items:
        naver_link = item.get("link", "")
        title = _clean_html(item.get("title", ""))
        description = _clean_html(item.get("description", ""))
        url = item.get("originallink") or naver_link
        pub_date = item.get("pubDate", "")

        record = {
            "collected_at": now,
            "topic": topic_key,
            "keyword": keyword,
            "query": query,
            "title": title,
            "description": description,
            "url": url,
            "naver_link": naver_link,
            "pub_date": pub_date,
            "accepted": False,
            "reject_reason": None,
            "score": None,
        }

        # 네이버 제휴 언론사 뉴스만 포함 (블로그·카페 제외)
        if not (naver_link.startswith("https://n.news.naver.com") or
                naver_link.startswith("https://news.naver.com")):
            record["reject_reason"] = "not_press"
            history_records.append(record)
            continue

        # 핵심어 게이트(옵션): 제목+본문에 토픽 핵심어가 하나도 없으면 컷.
        # STO 처럼 범용 금융어(증권·투자·토큰·블록체인)만으로 임계값을 넘겨
        # 일반 코인/증권사 뉴스가 새는 것을 막는다.
        if require_any:
            haystack = title + " " + description
            if not any(term in haystack for term in require_any):
                record["reject_reason"] = "no_core_term"
                history_records.append(record)
                continue

        # strict_match: 제목+내용에서 붙여쓰기로 2회 이상 등장해야 함
        if keyword in strict_match:
            combined = title + " " + description
            count = combined.count(keyword)
            record["keyword_count"] = count
            if count < 2:
                record["reject_reason"] = "keyword_too_few"
                history_records.append(record)
                continue

        # 가중 점수 기반 관련성 판정
        score = _relevance_score(title, description, positive_weights, negative_weights)
        record["score"] = score
        if score < threshold:
            record["reject_reason"] = "low_score"
            logger.debug(f"[필터:{topic_key}] 제외(점수 {score}): {title}")
            history_records.append(record)
            continue

        # 제목 게이트(옵션): 제목에 도메인 양성 키워드가 하나도 없으면 컷
        # → 본문에만 부동산 단어가 많은 정치/오프토픽 기사 차단
        if require_title:
            title_pos = sum(w for word, w in positive_weights.items() if w > 0 and word in title)
            if title_pos <= 0:
                record["reject_reason"] = "title_off_topic"
                logger.debug(f"[필터:{topic_key}] 제외(제목 비부동산): {title}")
                history_records.append(record)
                continue

        # ML 모델 보조 판정 (토픽별 모델 있을 때만)
        ml_prob = classifier.predict(title, description, topic_key)
        record["ml_prob"] = ml_prob
        if ml_prob is not None and ml_prob < 0.3:
            record["reject_reason"] = "ml_reject"
            record["accepted"] = False
            logger.debug(f"[ML:{topic_key}] 제외(확률 {ml_prob:.2f}): {title}")
            history_records.append(record)
            continue

        record["accepted"] = True
        history_records.append(record)
        results.append({
            "source": "naver",
            "keyword": keyword,
            "title": title,
            "url": url,
        })

    if history_records:
        history.save_batch(history_records, topic_key)

    logger.info(f"[Naver:{topic_key}] '{keyword}'(쿼리: '{query}') 결과: {len(results)}건 (전체 {len(raw_items)}건 중)")
    time.sleep(REQUEST_DELAY_SECONDS)
    return results
