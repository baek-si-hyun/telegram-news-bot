"""프로세스 생존 기록 — 정지 구간을 사후에라도 알아내기 위한 것.

2026-08-24~26 에 43시간 동안 봇이 멈췄는데 아무도 몰랐다. 원인은 코드가 아니라
호스트(Docker Desktop)가 꺼져 있던 것이었다. 봇이 죽어 있는 동안에는 봇이 알릴 수
없으므로, **복구된 시점에 "얼마나 비어 있었는지"를 알리는 것**이 현실적인 조치다.

매 주기 마지막에 시각을 남기고, 기동 시 그 값과 현재 시각의 차이를 본다.
"""

import json
import logging
import os
import tempfile
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE_DIR, "heartbeat.json")


def touch() -> None:
    """지금 살아 있다고 기록한다. 실패해도 본 흐름을 막지 않는다."""
    try:
        fd, tmp = tempfile.mkstemp(dir=BASE_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}, f)
        os.replace(tmp, PATH)
    except OSError as e:
        logger.debug(f"[Heartbeat] 기록 실패: {e}")


def gap_minutes():
    """직전 기록 이후 경과 분. 기록이 없거나 읽을 수 없으면 None."""
    if not os.path.exists(PATH):
        return None
    try:
        with open(PATH, encoding="utf-8") as f:
            at = json.load(f).get("at")
        if not at:
            return None
        return (datetime.now() - datetime.strptime(at, "%Y-%m-%dT%H:%M:%S")).total_seconds() / 60
    except (OSError, ValueError, json.JSONDecodeError):
        return None
