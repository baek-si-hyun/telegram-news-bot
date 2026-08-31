"""보낸 뉴스를 통합 포스팅 앱(unified-posting-all)에도 넘긴다.

이 봇은 뉴스를 **영속 저장하지 않는다.** 대기 큐는 보내고 나면 비우고(storage/pending.py),
캐시에는 중복 판별용으로 URL·제목키·시각만 남는다(storage/cache.py). 그래서 앱이 "봇이 보낸 뉴스"를
나중에 읽어 갈 방법이 없다 — 읽을 API 도, 공유 DB 도 없다.
그 자리를 메우는 것이 이 모듈이다: **보내는 순간 앱 서버에도 같이 적는다(dual-write).**

## 지켜야 할 것 — 텔레그램 전송을 절대 방해하지 않는다

앱 서버가 죽어 있든, 토큰이 틀렸든, 네트워크가 끊겼든 **뉴스는 텔레그램으로 계속 가야 한다.**
그래서 이 모듈의 모든 실패는 로그 한 줄로 끝난다(예외를 올리지 않는다). 타임아웃도 짧게 잡는다 —
30분 주기 안에서 수십 건을 보내는데 한 건이 붙잡히면 그 주기 전체가 밀린다.

## 켜고 끄기 — 환경변수 둘이 다 있어야만 켜진다

    UNIFIED_API_URL     예: http://host.docker.internal:18090/api/v1
    UNIFIED_INGEST_TOKEN  앱 서버의 NEWS_INGEST_TOKEN 과 같은 값

하나라도 없으면 **조용히 꺼진 채로 돈다.** 이 봇을 앱 없이 단독으로 돌리는 경우가 정상이기 때문이다.
(도커 안에서 호스트의 18090 에 닿으려면 localhost 가 아니라 host.docker.internal 이다.)

## 토픽 이름을 여기서 갈아 끼운다 — 중요

이 봇의 토픽 키는 'sto' 와 'realestate' 다. 그런데 통합 저장소에서 'realestate' 는 이미
**두 가지 다른 뜻**으로 쓰이고 있다(폴더 이름으로는 '링크 소재', DB 값으로는 '기사체 형식').
거기에 '진짜 부동산' 이라는 세 번째 뜻을 더하면, 그 저장소에서 가장 자주 나는 사고를 하나 더 만든다.
그래서 **경계인 여기서** 'realestate' → 'property' 로 바꿔 보낸다. 봇 안에서는 기존 이름 그대로다.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

# 앱 서버가 아는 토픽 이름. 왼쪽이 이 봇의 토픽 키, 오른쪽이 앱 쪽 이름이다.
# (앱의 '증시' 소분류는 아직 화면만 있고 이 봇에 대응 토픽이 없다 — 생기면 여기에 한 줄 는다.)
TOPIC_MAP = {
    "sto": "sto",
    "realestate": "property",
}

# 앱 서버가 느리거나 죽었을 때 이 봇이 붙잡히지 않도록. 전송 주기(30분) 안에서 수십 건이 돈다.
TIMEOUT_SEC = 3


def _config() -> tuple[str, str] | None:
    """환경변수 둘이 다 있을 때만 (URL, 토큰). 하나라도 없으면 꺼진 것으로 본다."""
    base = (os.getenv("UNIFIED_API_URL") or "").strip().rstrip("/")
    token = (os.getenv("UNIFIED_INGEST_TOKEN") or "").strip()
    if not base or not token:
        return None
    return base, token


def is_enabled() -> bool:
    return _config() is not None


def push(news: dict, topic_key: str) -> bool:
    """뉴스 한 건을 앱 서버에 적는다. **실패해도 예외를 올리지 않는다.**

    같은 URL 이 다시 오면 앱 서버가 무시한다(URL 이 유일 키다) — 이 봇이 중복을 걱정할 필요는 없다.
    돌려주는 값은 '앱에 적혔나' 이고, 텔레그램 전송 성패와는 아무 상관이 없다.
    """
    conf = _config()
    if conf is None:
        return False
    base, token = conf

    url = (news.get("url") or "").strip()
    title = (news.get("title") or "").strip()
    if not url or not title:
        # 제목이나 링크가 없으면 앱 화면에서 누를 수도, 글로 쓸 수도 없다 — 보낼 이유가 없다.
        return False

    payload = {
        "topic": TOPIC_MAP.get(topic_key, topic_key),
        "title": title,
        "url": url,
        # 'naver'(뉴스 검색) 또는 'molit'(국토부 보도자료). 화면이 출처를 그대로 보여준다.
        "source": (news.get("source") or "").strip() or "naver",
        # 국토부만 쓴다(철도·도로·택지·주택). 네이버 뉴스에는 없다.
        "category": (news.get("category") or "").strip() or None,
        # 네이버 뉴스만 쓴다 — 어떤 키워드로 걸린 기사인지.
        "keyword": (news.get("keyword") or "").strip() or None,
    }

    try:
        res = requests.post(
            f"{base}/news/ingest",
            json=payload,
            headers={"X-Ingest-Token": token},
            timeout=TIMEOUT_SEC,
        )
        if res.status_code >= 400:
            # 본문을 통째로 로그에 붓지 않는다 — 앞부분만으로 원인은 충분히 드러난다.
            logger.warning(f"[통합앱:{topic_key}] 적재 거절 {res.status_code}: {res.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        # 앱 서버가 없는 것은 이 봇에게 정상 상태다. 오류로 올리지 않는다.
        logger.warning(f"[통합앱:{topic_key}] 적재 실패: {e}")
        return False
