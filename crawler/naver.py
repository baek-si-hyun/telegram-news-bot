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


def _matched_terms(text: str, terms_by_length: list[str]) -> set[str]:
    """긴 항목부터 매칭하고, 잡힌 구간은 소거해 짧은 항목이 재매칭하지 못하게 한다.

    단순 `word in text` 로 전부 더하면 두 가지가 동시에 깨진다.
      · 상쇄  — '신분당선'(+6) 안의 '당선'(-6)이 같이 걸려 순증 0점이 된다.
      · 중복가산 — '부동산정책' 한 단어가 '부동산'+'정책'까지 세어 3중 계산된다.
    구간을 소거하면 가장 구체적인 항목 하나만 점수에 반영된다.
    """
    if not text:
        return set()
    remaining = text
    found = set()
    for term in terms_by_length:
        if term and term in remaining:
            found.add(term)
            remaining = remaining.replace(term, "\x00" * len(term))
    return found


def _relevance_score(title: str, description: str,
                     positive_weights: dict, negative_weights: dict,
                     terms_by_length: list[str] | None = None) -> int:
    """제목+설명의 도메인 관련성 점수 계산 (양수=관련, 음수=무관)

    제목에 있는 단어는 2배 가중치 (제목이 기사 핵심을 반영하므로).
    양성·음성을 한 번에 매칭해야 상쇄 문제가 생기지 않으므로 병합해서 처리한다.
    """
    merged = {**positive_weights, **negative_weights}
    if terms_by_length is None:
        terms_by_length = sorted(merged, key=len, reverse=True)

    title_hits = _matched_terms(title, terms_by_length)
    desc_hits = _matched_terms(description, terms_by_length)

    score = 0
    for term in title_hits:
        score += merged[term] * 2
    for term in desc_hits - title_hits:
        score += merged[term]
    return score


# 이미 히스토리에 남긴 (토픽, URL) — 같은 기사가 30분마다 다시 검색돼도 재기록하지 않는다.
# 이 억제가 없어서 history/*.jsonl 이 3.2GB 까지 불었고 그중 97.9% 가 같은 URL 의 중복 행이었다.
_history_seen: set[tuple[str, str]] = set()
_HISTORY_SEEN_MAX = 200_000


def _remember(record: dict, topic_key: str, sink: list[dict]) -> None:
    """이 기록을 히스토리에 남길지 판단 (통과·탈락 공통).

    · not_press 는 아예 남기지 않는다 — 유일한 소비자인 ml/train_model.py 가 명시적으로
      건너뛰므로(load_data) 읽는 쪽이 없는 순수 사표 데이터였다(전체 행의 65%).
    · 나머지는 URL 당 1회만 남긴다(프로세스 수명 기준). 같은 기사가 30분마다 다시
      검색돼 재기록되던 것이 중복 행 97.9% 의 원인이었다.
    """
    if record.get("reject_reason") == "not_press":
        return
    key = (topic_key, record.get("url") or "")
    if key in _history_seen:
        return
    if len(_history_seen) < _HISTORY_SEEN_MAX:
        _history_seen.add(key)
    sink.append(record)


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
    require_any = profile.get("require_any_terms")  # 핵심어 게이트(옵션)
    # 제목 게이트: 제목에 '이 가중치 이상'인 도메인어가 있어야 통과. 0/미설정이면 게이트 없음.
    # (구버전 require_title_keyword=True 는 가중치 1짜리 범용어로도 통과해 사실상 무력했다)
    title_gate_min = profile.get("title_gate_min_weight", 0)
    if not title_gate_min and profile.get("require_title_keyword"):
        title_gate_min = 1
    # 제휴사 게이트 우회 대상: 사용자가 직접 고른 고유명사. 제목에 있으면 비제휴 언론사도 허용.
    bypass_terms = profile.get("partner_bypass_terms") or ()
    # 모델 판정 시 이 고유명사가 제목에 있으면 훨씬 낮은 기준을 쓴다(사용자 우선순위 반영).
    # 절대값(ml_priority_cutoff)을 쓰는 이유: 기본 컷오프를 조정할 때마다 우선순위 기준이
    # 같이 흔들리면 안 된다. "내가 고른 키워드가 제목에 있으면 이 선만 넘으면 보낸다"가
    # 독립적으로 유지되어야 한다.
    priority_terms = profile.get("ml_priority_terms") or bypass_terms
    priority_cut = profile.get("ml_priority_cutoff")
    priority_delta = profile.get("ml_priority_cutoff_delta", 0.12)
    # 본문 2단계를 쓰는 토픽이면 수집 단계는 잠정선만 적용한다.
    provisional_cut = None
    if profile.get("use_body_model") and classifier.has_body_model(topic_key):
        provisional_cut = profile.get("ml_provisional_cutoff", 0.35)

    merged_weights = {**positive_weights, **negative_weights}
    terms_by_length = sorted(merged_weights, key=len, reverse=True)

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

        # 네이버 제휴 언론사만 통과가 기본값.
        # 단, 이 게이트가 지역 언론을 통째로 잘라내 사용자 고유명사 키워드(인주역·시리물류단지
        # 등)를 고사시켰다. 그래서 제목에 고유명사가 있으면 비제휴사도 통과시킨다.
        # 광범위 키워드(주택공급 등)는 계속 제휴사로 제한되므로 노이즈는 늘지 않는다.
        if not (naver_link.startswith("https://n.news.naver.com") or
                naver_link.startswith("https://news.naver.com")):
            if not any(term in title for term in bypass_terms):
                record["reject_reason"] = "not_press"
                _remember(record, topic_key, history_records)
                continue
            record["partner_bypass"] = True

        # 핵심어 게이트(옵션): 제목+본문에 토픽 핵심어가 하나도 없으면 컷.
        # STO 처럼 범용 금융어(증권·투자·토큰·블록체인)만으로 임계값을 넘겨
        # 일반 코인/증권사 뉴스가 새는 것을 막는다.
        if require_any:
            haystack = title + " " + description
            if not any(term in haystack for term in require_any):
                record["reject_reason"] = "no_core_term"
                _remember(record, topic_key, history_records)
                continue

        # strict_match: 제목+내용에서 붙여쓰기로 2회 이상 등장해야 함
        if keyword in strict_match:
            combined = title + " " + description
            count = combined.count(keyword)
            record["keyword_count"] = count
            if count < 2:
                record["reject_reason"] = "keyword_too_few"
                _remember(record, topic_key, history_records)
                continue

        # 가중 점수는 항상 계산해 기록한다(분석·학습용). 판정에 쓸지는 아래에서 갈린다.
        score = _relevance_score(title, description, positive_weights,
                                 negative_weights, terms_by_length)
        record["score"] = score

        # ── 관련성 판정 ──
        # 모델이 있으면 모델이 판정하고, 없으면 가중치 규칙으로 판정한다.
        #
        # 모델을 규칙 '위에 얹지' 않고 '대체'하는 이유: 규칙 AND 모델로 묶으면 규칙의
        # 낮은 재현율(실측 56%)이 그대로 상한이 되어 모델 단독보다 나빠진다
        # (실측 F1 규칙 0.579 / 하이브리드 0.611 / 모델 단독 0.782).
        # 제휴사 게이트·핵심어 게이트는 판정이 아니라 출처·도메인 위생이라 위에 그대로 둔다.
        ml_prob = classifier.predict(title, description, topic_key, score)
        record["ml_prob"] = ml_prob

        if ml_prob is not None:
            # 본문 모델이 있으면 여기서는 '잠정 통과선'만 넘기고 최종 판정은 본문 단계에서
            # 한다. 잠정선을 낮게 잡아야 본문이 살릴 수 있는 기사를 여기서 잃지 않는다.
            # (스니펫 모델 재현율: 잠정선 0.35 에서 약 88%)
            cut = provisional_cut if provisional_cut is not None else (classifier.cutoff(topic_key) or 0.5)
            # 사용자가 직접 고른 고유명사가 제목에 있으면 컷오프를 낮춘다.
            # 학습 데이터에서 이런 고유명사는 기관 홍보 기사로 더 자주 등장해 모델이
            # 과소평가하는 경향이 있다(예: '국가철도공단'). 우회가 아니라 가산점이라
            # 홍보 기사(확률 0.06)는 여전히 걸러진다.
            if priority_terms and any(t in title for t in priority_terms):
                cut = priority_cut if priority_cut is not None else max(0.0, cut - priority_delta)
                record["ml_priority"] = True
            if ml_prob < cut:
                record["reject_reason"] = "ml_reject"
                logger.debug(f"[ML:{topic_key}] 제외(확률 {ml_prob:.2f} < {cut:.2f}): {title}")
                _remember(record, topic_key, history_records)
                continue
        else:
            if score < threshold:
                record["reject_reason"] = "low_score"
                logger.debug(f"[필터:{topic_key}] 제외(점수 {score}): {title}")
                _remember(record, topic_key, history_records)
                continue

            # 제목 게이트(옵션): 제목에 '충분히 구체적인' 도메인어가 있어야 통과.
            if title_gate_min:
                title_hits = _matched_terms(title, terms_by_length)
                if not any(merged_weights.get(t, 0) >= title_gate_min for t in title_hits):
                    record["reject_reason"] = "title_off_topic"
                    logger.debug(f"[필터:{topic_key}] 제외(제목 비도메인): {title}")
                    _remember(record, topic_key, history_records)
                    continue

        record["accepted"] = True
        _remember(record, topic_key, history_records)
        results.append({
            "source": "naver",
            "keyword": keyword,
            "title": title,
            "url": url,
            # 본문 재판정 단계로 넘길 값들
            "description": description,
            "ml_prob": ml_prob,
            "rule_score": score,
            "ml_priority": bool(record.get("ml_priority")),
        })

    if history_records:
        history.save_batch(history_records, topic_key)

    logger.info(f"[Naver:{topic_key}] '{keyword}'(쿼리: '{query}') 결과: {len(results)}건 (전체 {len(raw_items)}건 중)")
    time.sleep(REQUEST_DELAY_SECONDS)
    return results
