import logging
import unified
from crawler import naver, molit
from bot import telegram, commands
from storage import cache, pending, keywords as kwstore
from config import active_topics

logger = logging.getLogger(__name__)


def _collect_topic(topic_key: str, profile: dict):
    """단일 토픽 수집: 뉴스 검색 → 중복 제거 → 대기 큐 저장"""
    # 사용자가 /add·/del 로 넣고 뺀 키워드를 config 위에 병합(매 주기 최신 반영)
    profile = kwstore.apply(profile, topic_key)
    logger.info(f"[{topic_key}] 뉴스 수집 시작 (키워드 {len(profile['keywords'])}개)")
    collected_this_cycle = set()

    for keyword, query in profile["keywords"].items():
        try:
            results = naver.search(keyword, query, profile, topic_key)
        except Exception as e:
            logger.error(f"[수집:{topic_key}] '{keyword}' 검색 오류: {e}")
            continue

        new_news = cache.filter_new(results, topic_key)
        new_news = [n for n in new_news if n.get("url") not in collected_this_cycle]

        if not new_news:
            logger.info(f"[{topic_key}] '{keyword}' 신규 뉴스 없음")
            continue

        pending.add(new_news, topic_key)
        cache.mark_all_sent(new_news, topic_key)
        for n in new_news:
            collected_this_cycle.add(n.get("url"))

        logger.info(f"[{topic_key}] '{keyword}' {len(new_news)}건 수집")

    logger.info(f"[{topic_key}] 뉴스 수집 완료")


def _send_topic(topic_key: str, profile: dict):
    """단일 토픽 전송: 대기 큐 → 해당 토픽 텔레그램 채널"""
    news_list = pending.pop_all(topic_key)
    if not news_list:
        logger.info(f"[{topic_key}] 전송할 뉴스 없음")
        return

    logger.info(f"[{topic_key}] 텔레그램 전송 시작: {len(news_list)}건")
    sent = 0
    for news in news_list:
        if telegram.send_news(news, profile["bot_token"], profile["chat_ids"]):
            sent += 1
            # 통합 포스팅 앱에도 같이 적는다. 실패해도 여기서 멈추지 않는다(unified.push 는 예외를 올리지 않는다).
            unified.push(news, topic_key)
    logger.info(f"[{topic_key}] 텔레그램 전송 완료: {sent}/{len(news_list)}건")


def _molit_topic(topic_key: str, profile: dict):
    """국토부 보도자료 수집 → 즉시 전송 (뉴스보다 먼저, 카테고리 우선순위 순).

    네이버 뉴스와 달리 관련성 점수 필터를 태우지 않는다 — 국토부 공식 발표이고
    담당부서로 이미 선별되므로, 점수 필터에 걸려 잘리면 안 된다.
    """
    categories = profile.get("molit_categories") or []
    if not categories:
        return

    items = molit.fetch_new(topic_key, categories)
    if not items:
        return

    logger.info(f"[MOLIT:{topic_key}] 보도자료 전송 시작: {len(items)}건")
    sent = 0
    for news in items:
        if telegram.send_news(news, profile["bot_token"], profile["chat_ids"]):
            sent += 1
            # 국토부 보도자료도 앱으로 같이 넘긴다 — history JSONL 에는 네이버 기사만 남아서,
            # 여기서 안 넘기면 앱의 부동산 목록에서 보도자료만 통째로 빠진다.
            unified.push(news, topic_key)
    logger.info(f"[MOLIT:{topic_key}] 보도자료 전송 완료: {sent}/{len(items)}건")


def _alert(topic_key: str, profile: dict, text: str) -> None:
    """운영 장애를 해당 토픽 채널로 알린다.

    telegram.send_alert 는 구현돼 있었지만 호출부가 한 곳도 없어서, 43시간 정지가
    아무에게도 통보되지 않았다. 알림 전송 자체가 실패해도 본 흐름을 막지 않는다.
    """
    try:
        telegram.send_alert(text, profile["bot_token"], profile["chat_ids"])
    except Exception as e:  # noqa: BLE001
        logger.error(f"[알림:{topic_key}] 알림 전송 실패: {e}")


def run_cycle():
    """30분마다 실행: 수집 → 즉시 전송.

    토픽마다 (1) 국토부 보도자료 먼저, (2) 네이버 뉴스 순으로 보낸다.
    (과거에는 08:00/13:00/19:00 배치 전송이었으나, 속보성을 위해 즉시 전송으로 변경)
    """
    for topic_key, profile in active_topics().items():
        try:
            _molit_topic(topic_key, profile)
        except Exception as e:
            logger.error(f"[MOLIT:{topic_key}] 처리 중 오류: {e}")
            _alert(topic_key, profile, f"국토부 보도자료 처리 실패: {e}")

        try:
            _collect_topic(topic_key, profile)
        except Exception as e:
            logger.error(f"[수집:{topic_key}] 처리 중 오류: {e}")
            _alert(topic_key, profile, f"뉴스 수집 실패 — 이번 주기 건너뜀: {e}")
            continue

        try:
            _send_topic(topic_key, profile)
        except Exception as e:
            logger.error(f"[전송:{topic_key}] 처리 중 오류: {e}")
            _alert(topic_key, profile, f"텔레그램 전송 실패: {e}")


def collect():
    """(수동용) 모든 활성 토픽 수집만"""
    for topic_key, profile in active_topics().items():
        try:
            _collect_topic(topic_key, profile)
        except Exception as e:
            logger.error(f"[수집:{topic_key}] 처리 중 오류: {e}")


def send():
    """(수동용) 대기 큐에 쌓인 뉴스 전송"""
    for topic_key, profile in active_topics().items():
        try:
            _send_topic(topic_key, profile)
        except Exception as e:
            logger.error(f"[전송:{topic_key}] 처리 중 오류: {e}")


def send_help():
    """키워드 명령(/add·/del·/list) 사용법 안내를 모든 활성 토픽 채널로 전송.

    기존 홍보(send_promos) 대신 이 함수를 스케줄에 건다(main.py).
    """
    text = commands.help_message()
    for topic_key, profile in active_topics().items():
        try:
            telegram.send_promo(profile["bot_token"], profile["chat_ids"], [text])
            logger.info(f"[{topic_key}] 명령 안내 전송 완료")
        except Exception as e:
            logger.error(f"[안내:{topic_key}] 처리 중 오류: {e}")


def send_promos():
    """(보류) 홍보 메시지: 모든 활성 토픽의 채널로 각자 전송.

    현재 스케줄에서 제외됨 — 명령 안내(send_help)로 교체. 되돌리려면 main.py 의
    스케줄 등록을 send_help → send_promos 로 바꾸면 된다.
    """
    for topic_key, profile in active_topics().items():
        messages = profile.get("promo") or []
        if not messages:
            continue
        try:
            telegram.send_promo(profile["bot_token"], profile["chat_ids"], messages)
            logger.info(f"[{topic_key}] 홍보 메시지 전송 완료")
        except Exception as e:
            logger.error(f"[홍보:{topic_key}] 처리 중 오류: {e}")
