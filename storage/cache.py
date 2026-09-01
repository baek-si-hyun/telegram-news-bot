import json
import logging
import os
import re
import tempfile
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from config import CACHE_EXPIRE_DAYS

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 제목 유사도 임계값 (0.0~1.0).
# 실측 라벨셋에서 '다른 기사' 쌍의 최대 ratio 가 0.417 이라 0.55 는 원래 안전했다.
# 오탐의 원인은 이 값이 아니라 아래 최장공통부분 규칙이었으므로 그대로 둔다.
SIMILARITY_THRESHOLD = 0.55

# 키워드 겹침 비율 임계값 (2글자 n-gram 집합 기준).
# 라벨셋 분리점: '다른 기사' 최대 0.364 vs '같은 사안' 최소 0.467 → 0.45.
WORD_OVERLAP_THRESHOLD = 0.45

# 최장 공통부분 판정의 최소 길이 및 짧은 제목 대비 최소 비율.
# 예전에는 '7글자만 겹치면 중복'이라 '원조부촌워커힐'·'토지거래허가구역'·'부동산AtoZ'
# 같은 흔한 어구 하나로 전혀 다른 기사가 무더기로 폐기됐다(8월 통과분의 47%).
MIN_LCS_LEN = 12
MIN_LCS_RATIO = 0.6

# 유사도 비교 대상 최대 건수 (성능 보호)
MAX_SIMILARITY_COMPARE = 500


def _cache_file(topic: str) -> str:
    """토픽별 캐시 파일 경로 (storage/<topic>/sent_news.json)"""
    topic_dir = os.path.join(BASE_DIR, topic)
    os.makedirs(topic_dir, exist_ok=True)
    return os.path.join(topic_dir, "sent_news.json")


def _normalize_title(title: str) -> str:
    """제목에서 공백·특수문자 제거 후 소문자화"""
    return re.sub(r"[^\w가-힣]", "", title).lower()


def _extract_words(normalized: str) -> set[str]:
    """정규화된 제목을 2글자 n-gram 단위로 분할하여 키워드 셋 생성"""
    words = set()
    for segment in re.findall(r"[가-힣]+", normalized):
        if len(segment) >= 2:
            words.add(segment)
            for i in range(len(segment) - 1):
                words.add(segment[i:i+2])
    return words


def _is_similar(title_a: str, title_b: str) -> bool:
    """두 정규화된 제목이 유사한지 판별 (시퀀스 유사도 + 키워드 겹침).

    목적은 '같은 사안을 여러 언론사가 쓴 기사'를 한 건으로 묶는 것이지, 흔한 어구를
    공유하는 별개 기사를 지우는 것이 아니다. 세 규칙 모두 후자로 기울어 있었다.
    """
    if not title_a or not title_b:
        return False
    if SequenceMatcher(None, title_a, title_b).ratio() >= SIMILARITY_THRESHOLD:
        return True
    words_a = _extract_words(title_a)
    words_b = _extract_words(title_b)
    if words_a and words_b:
        overlap = len(words_a & words_b)
        min_len = min(len(words_a), len(words_b))
        if overlap / min_len >= WORD_OVERLAP_THRESHOLD:
            return True
    # 최장 공통부분은 '길이 12자 이상'이면서 '짧은 쪽 제목의 60% 이상'일 때만 중복으로 본다.
    # 고정 7자 기준은 지역명·제도명 한 덩어리만 겹쳐도 걸려 오탐이 압도적이었다.
    match = SequenceMatcher(None, title_a, title_b).find_longest_match(0, len(title_a), 0, len(title_b))
    shorter = min(len(title_a), len(title_b))
    return match.size >= MIN_LCS_LEN and match.size >= shorter * MIN_LCS_RATIO


def _load(topic: str) -> dict:
    path = _cache_file(topic)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[Cache:{topic}] 캐시 파일 로드 실패, 초기화: {e}")
        return {}


def _save(topic: str, data: dict) -> None:
    """원자적 파일 쓰기 (임시 파일 → rename)"""
    path = _cache_file(topic)
    try:
        dir_name = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        logger.error(f"[Cache:{topic}] 캐시 저장 실패: {e}")


def _purge_expired(data: dict, days: int = CACHE_EXPIRE_DAYS) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    result = {}
    for k, v in data.items():
        if isinstance(v, dict):
            sent_at = v.get("sent_at", "")[:19]
            if sent_at >= cutoff:
                result[k] = v
    return result


def _is_duplicate(item: dict, url_set: set, title_list: list[str]) -> bool:
    """URL 완전 일치 또는 제목 유사도 기반 중복 판별"""
    url = item.get("url", "")
    title_key = _normalize_title(item.get("title", ""))

    if url and url in url_set:
        return True

    compare_targets = title_list[-MAX_SIMILARITY_COMPARE:]
    for existing_title in compare_targets:
        if _is_similar(title_key, existing_title):
            return True

    return False


def filter_new(news_list: list[dict], topic: str) -> list[dict]:
    """이미 전송된 뉴스를 제외하고 신규 뉴스만 반환 (토픽별 캐시 기준)"""
    data = _load(topic)
    data = _purge_expired(data)

    url_set = {v["url"] for v in data.values() if isinstance(v, dict) and v.get("url")}
    title_list = [v["title_key"] for v in data.values() if isinstance(v, dict) and v.get("title_key")]

    new_items = []
    seen_urls = set(url_set)
    seen_titles = list(title_list)

    for item in news_list:
        if _is_duplicate(item, seen_urls, seen_titles):
            continue
        new_items.append(item)
        url = item.get("url", "")
        title_key = _normalize_title(item.get("title", ""))
        if url:
            seen_urls.add(url)
        if title_key:
            seen_titles.append(title_key)

    logger.info(f"[Cache:{topic}] 전체 {len(news_list)}건 중 신규 {len(new_items)}건")
    return new_items


# '본 URL' 보관 기간. 국토부 목록 30건 ≈ 4일치라, 7일(CACHE_EXPIRE_DAYS)이면
# 만료된 URL이 아직 목록에 남아 재전송될 수 있다. 넉넉히 30일 보관.
SEEN_EXPIRE_DAYS = 30


def _ns_file(topic: str, ns: str) -> str:
    """네임스페이스별 '본 URL' 파일 (storage/<topic>/<ns>_seen.json)"""
    topic_dir = os.path.join(BASE_DIR, topic)
    os.makedirs(topic_dir, exist_ok=True)
    return os.path.join(topic_dir, f"{ns}_seen.json")


def _load_ns(topic: str, ns: str) -> dict:
    path = _ns_file(topic, ns)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[Seen:{topic}/{ns}] 로드 실패, 초기화: {e}")
        return {}


def _save_ns(topic: str, ns: str, data: dict) -> None:
    path = _ns_file(topic, ns)
    try:
        dir_name = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        logger.error(f"[Seen:{topic}/{ns}] 저장 실패: {e}")


def filter_new_urls(items: list[dict], topic: str, ns: str) -> list[dict]:
    """URL 완전일치로만 신규 판별 (제목 유사도는 쓰지 않음).

    국토부 보도자료용. 같은 사안을 다룬 네이버 기사와 제목이 비슷하다는 이유로
    공식 발표가 누락되면 안 되므로, 유사도 기반 중복제거를 적용하지 않는다.
    """
    seen = _load_ns(topic, ns)
    seen = _purge_expired(seen, SEEN_EXPIRE_DAYS)

    new_items = []
    batch_urls = set()
    for item in items:
        url = item.get("url", "")
        if not url or url in seen or url in batch_urls:
            continue
        batch_urls.add(url)
        new_items.append(item)
    return new_items


def mark_urls_sent(items: list[dict], topic: str, ns: str) -> None:
    """URL들을 '본 것'으로 기록 (filter_new_urls 와 짝)"""
    data = _load_ns(topic, ns)
    data = _purge_expired(data, SEEN_EXPIRE_DAYS)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for item in items:
        url = item.get("url", "")
        if url:
            data[url] = {"url": url, "sent_at": now}
    _save_ns(topic, ns, data)


def mark_all_sent(news_list: list[dict], topic: str) -> None:
    """뉴스 목록 전체를 전송 완료로 기록 (토픽별 캐시)"""
    data = _load(topic)
    data = _purge_expired(data)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for item in news_list:
        url = item.get("url", "")
        title_key = _normalize_title(item.get("title", ""))
        if url:
            data[url] = {"url": url, "title_key": title_key, "sent_at": now}
    _save(topic, data)
    logger.info(f"[Cache:{topic}] {len(news_list)}건 캐시 저장 완료")
