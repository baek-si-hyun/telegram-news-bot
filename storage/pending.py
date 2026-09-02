"""전송 대기 뉴스 큐 관리 (토픽별 분리)"""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _pending_file(topic: str) -> str:
    """토픽별 대기 큐 파일 경로 (storage/<topic>/pending_news.json)"""
    topic_dir = os.path.join(BASE_DIR, topic)
    os.makedirs(topic_dir, exist_ok=True)
    return os.path.join(topic_dir, "pending_news.json")


def _load(topic: str) -> list[dict]:
    path = _pending_file(topic)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[Pending:{topic}] 파일 로드 실패, 초기화: {e}")
        return []


def _save(topic: str, data: list[dict]) -> None:
    path = _pending_file(topic)
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
        logger.error(f"[Pending:{topic}] 저장 실패: {e}")


def add(news_list: list[dict], topic: str) -> None:
    """대기 큐에 뉴스 추가 (URL + 유사 제목 중복 방지)"""
    if not news_list:
        return
    from storage.cache import _normalize_title, _is_similar

    data = _load(topic)
    existing_urls = {item.get("url") for item in data}
    existing_titles = [_normalize_title(item.get("title", "")) for item in data]

    added = 0
    for item in news_list:
        url = item.get("url", "")
        if url in existing_urls:
            continue
        title_key = _normalize_title(item.get("title", ""))
        if any(_is_similar(title_key, t) for t in existing_titles):
            continue
        data.append(item)
        existing_urls.add(url)
        existing_titles.append(title_key)
        added += 1

    _save(topic, data)
    if added:
        logger.info(f"[Pending:{topic}] {added}건 대기 큐에 추가 (총 {len(data)}건)")


def pop_all(topic: str) -> list[dict]:
    """대기 큐의 모든 뉴스를 꺼내고 큐를 비움.

    ⚠ 전송 전에 큐를 비우므로 전송 도중 프로세스가 죽으면 그 배치가 영구 유실된다.
    새 코드는 peek_all() + remove_sent() 를 쓸 것. 하위 호환용으로만 남긴다.
    """
    data = _load(topic)
    if data:
        _save(topic, [])
    return data


def peek_all(topic: str) -> list[dict]:
    """큐를 비우지 않고 내용만 읽는다."""
    return _load(topic)


def remove_sent(urls, topic: str) -> None:
    """전송에 성공한 URL 만 큐에서 제거한다.

    pop_all 처럼 미리 비우지 않기 때문에, 전송 중 프로세스가 죽어도 못 보낸 건이
    큐에 남아 다음 주기에 다시 시도된다.
    """
    sent = {u for u in urls if u}
    if not sent:
        return
    data = _load(topic)
    remain = [item for item in data if item.get("url") not in sent]
    if len(remain) != len(data):
        _save(topic, remain)
    if remain:
        logger.info(f"[Pending:{topic}] {len(data) - len(remain)}건 제거, {len(remain)}건 미전송 잔류")
