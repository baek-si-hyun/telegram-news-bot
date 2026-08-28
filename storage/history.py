import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _history_dir(topic: str) -> str:
    """토픽별 히스토리 디렉터리 (storage/<topic>/history)"""
    d = os.path.join(BASE_DIR, topic, "history")
    os.makedirs(d, exist_ok=True)
    return d


def _get_filepath(topic: str) -> str:
    """월별 파일로 분리 (예: storage/<topic>/history/2026-06.jsonl)"""
    return os.path.join(_history_dir(topic), f"{datetime.now().strftime('%Y-%m')}.jsonl")


def save(record: dict, topic: str) -> None:
    """뉴스 1건의 수집 기록을 JSONL 형식으로 저장"""
    try:
        with open(_get_filepath(topic), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error(f"[History:{topic}] 기록 저장 실패: {e}")


def save_batch(records: list[dict], topic: str) -> None:
    """여러 건을 한번에 저장"""
    try:
        with open(_get_filepath(topic), "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error(f"[History:{topic}] 배치 저장 실패: {e}")
