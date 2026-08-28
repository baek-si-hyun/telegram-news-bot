"""사용자 추가/삭제 키워드 저장소 (토픽별)

config.py 의 TOPICS[topic]["keywords"] 는 코드에 고정된 '기본 키워드'다.
이 모듈은 그 위에 사용자가 텔레그램 명령(/add, /del)으로 넣고 뺀 변경분을
storage/<topic>/keywords.json 에 저장하고, 수집 시점에 병합(apply)한다.

저장 스키마:
    {
      "added":    { "판교역": {"query": "판교역", "weight": 6}, ... },
      "disabled": [ "리턴플러스", ... ]   # 기본 키워드 중 끈 것
    }

병합 규칙(apply):
    keywords         = 기본 ∪ added,  이후 disabled 제거
    positive_weights = 기본 ∪ added(weight)  (added 가 같은 키를 덮어씀)
"""

import json
import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# read-modify-write 보호 (명령 스레드에서 동시 호출 방지)
_lock = threading.Lock()


def _store_file(topic: str) -> str:
    topic_dir = os.path.join(BASE_DIR, topic)
    os.makedirs(topic_dir, exist_ok=True)
    return os.path.join(topic_dir, "keywords.json")


def _empty() -> dict:
    return {"added": {}, "disabled": []}


def _load(topic: str) -> dict:
    path = _store_file(topic)
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[Keywords:{topic}] 로드 실패, 초기화: {e}")
        return _empty()
    # 스키마 정규화(손상/구버전 방어)
    if not isinstance(data, dict):
        return _empty()
    added = data.get("added")
    disabled = data.get("disabled")
    return {
        "added": added if isinstance(added, dict) else {},
        "disabled": disabled if isinstance(disabled, list) else [],
    }


def _save(topic: str, data: dict) -> None:
    path = _store_file(topic)
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
        logger.error(f"[Keywords:{topic}] 저장 실패: {e}")


def default_weight(profile: dict) -> int:
    """추가 키워드의 자동 가중치.

    사용자가 가중치를 지정하지 않으므로, 그 토픽의 기존 '검색 키워드'들이 가진
    가중치의 중앙값으로 맞춘다(다른 검색어들과 비슷한 수준 → STO·부동산 모두 대략 6).
    데이터가 없으면 ceil(threshold/2) 로 폴백(제목에 있으면 통과하는 최소값).
    """
    kw = profile.get("keywords", {})
    pw = profile.get("positive_weights", {})
    weights = sorted(pw[k] for k in kw if pw.get(k, 0) > 0)
    if weights:
        return weights[len(weights) // 2]  # 중앙값
    threshold = int(profile.get("threshold", 6))
    return max(1, (threshold + 1) // 2)


def add(topic: str, keyword: str, query: str, weight: int) -> None:
    """키워드 추가/수정. disabled 에 있었다면 되살림."""
    with _lock:
        data = _load(topic)
        data["added"][keyword] = {"query": query, "weight": int(weight)}
        if keyword in data["disabled"]:
            data["disabled"] = [k for k in data["disabled"] if k != keyword]
        _save(topic, data)
    logger.info(f"[Keywords:{topic}] 추가: '{keyword}' (query='{query}', weight={weight})")


def remove(topic: str, keyword: str, config_keywords: set[str]) -> str:
    """키워드 삭제.

    반환값:
      "removed"          - 사용자가 추가했던 키워드를 삭제함
      "disabled"         - 기본(config) 키워드를 비활성 처리함
      "already_disabled" - 이미 비활성 상태인 기본 키워드
      "not_found"        - 어디에도 없는 키워드
    """
    with _lock:
        data = _load(topic)
        if keyword in data["added"]:
            del data["added"][keyword]
            _save(topic, data)
            logger.info(f"[Keywords:{topic}] 삭제(추가분): '{keyword}'")
            return "removed"
        if keyword in config_keywords:
            if keyword in data["disabled"]:
                return "already_disabled"
            data["disabled"].append(keyword)
            _save(topic, data)
            logger.info(f"[Keywords:{topic}] 비활성(기본): '{keyword}'")
            return "disabled"
        return "not_found"


def snapshot(topic: str) -> tuple[dict, list[str]]:
    """(added, disabled) 반환 — /list 표시용."""
    data = _load(topic)
    return data["added"], data["disabled"]


def apply(profile: dict, topic: str) -> dict:
    """config 프로필에 저장소 변경분을 병합한 '유효 프로필' 사본을 반환.

    원본 profile 은 변경하지 않는다(수집 스레드에서 매 주기 호출).
    """
    data = _load(topic)
    added = data["added"]
    disabled = data["disabled"]
    if not added and not disabled:
        return profile  # 변경 없음 → 원본 그대로(빠른 경로)

    keywords = dict(profile.get("keywords", {}))
    positive = dict(profile.get("positive_weights", {}))

    for kw, meta in added.items():
        keywords[kw] = meta.get("query", kw)
        positive[kw] = int(meta.get("weight", default_weight(profile)))

    for kw in disabled:
        keywords.pop(kw, None)

    merged = dict(profile)
    merged["keywords"] = keywords
    merged["positive_weights"] = positive
    return merged
