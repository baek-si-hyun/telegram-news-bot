import logging
import unified
from crawler import naver, molit, body
from bot import telegram, commands
from ml import classifier
from storage import cache, pending, heartbeat, keywords as kwstore
from config import active_topics

logger = logging.getLogger(__name__)

# 본문까지 보고 판정을 끝낸 URL 을 기억하는 네임스페이스 (storage/<topic>/body_eval_seen.json).
# 탈락분은 sent_news 캐시에 남지 않으므로 이게 없으면 같은 기사의 본문을 매 주기 다시 받는다.
BODY_EVAL_NS = "body_eval"


def _refine_with_body(news_list: list[dict], topic_key: str, profile: dict) -> list[dict]:
    """본문을 받아 최종 판정한다 (본문 모델이 있는 토픽만).

    수집 단계는 '잠정 통과선'만 적용했다. 여기서 본문을 받아 다시 판정해 최종 결정을 내린다.
    제목+스니펫 121자만으로는 '부동산 정책 기사'와 '부동산을 소재로 쓴 정치 기사'가 잘
    안 갈리는데, 본문을 넣으면 같은 정밀도(82%)에서 재현율이 62.2% → 68.3% 로 오른다.

    본문을 못 받거나 본문 모델이 실패하면 **스니펫 판정을 그대로 쓴다** — 외부 사이트
    사정이 뉴스 전송을 막지 않아야 한다.
    """
    if not (profile.get("use_body_model") and classifier.has_body_model(topic_key)):
        return news_list
    if not news_list:
        return news_list

    # 이미 본문까지 보고 탈락시킨 기사는 다시 받지 않는다.
    # 탈락분은 캐시(sent_news)에 남지 않으므로, 이 기억이 없으면 같은 기사의 본문을
    # 30분마다 계속 다시 받게 된다.
    news_list = cache.filter_new_urls(news_list, topic_key, BODY_EVAL_NS)
    if not news_list:
        return news_list

    snip_cut = classifier.cutoff(topic_key) or 0.5
    body_cut = classifier.body_cutoff(topic_key) or 0.5
    prio_cut = profile.get("ml_priority_cutoff")

    bodies = body.fetch_many([n.get("url") for n in news_list])

    kept, dropped = [], 0
    for n in news_list:
        text = bodies.get(n.get("url", ""), "")
        p_body = classifier.predict_body(n.get("title", ""), n.get("description", ""),
                                         topic_key, text, n.get("rule_score"))
        if p_body is None:
            # 본문 없음 → 스니펫 확률로 최종 판정 (수집 때는 잠정선만 통과한 상태)
            prob, cut, src = n.get("ml_prob"), snip_cut, "스니펫"
        else:
            prob, cut, src = p_body, body_cut, "본문"
        if n.get("ml_priority") and prio_cut is not None:
            cut = prio_cut
        if prob is None or prob >= cut:
            n["final_prob"] = prob
            n["decided_by"] = src
            kept.append(n)
        else:
            dropped += 1

    # 통과·탈락 모두 '판정 끝'으로 기록한다. 통과분은 전송 후 sent_news 에도 남지만,
    # 여기에 같이 남겨야 전송 실패로 큐에 머무는 동안 본문을 또 받지 않는다.
    cache.mark_urls_sent(news_list, topic_key, BODY_EVAL_NS)
    logger.info(f"[{topic_key}] 본문 재판정: {len(news_list)}건 → 통과 {len(kept)}건 (컷 {dropped}건)")
    return kept


def _collect_topic(topic_key: str, profile: dict):
    """단일 토픽 수집: 뉴스 검색 → 중복 제거 → 본문 재판정 → 대기 큐 저장"""
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

        # 본문 재판정은 중복 제거 '뒤'에 둔다 — 매 주기 1,200건을 다 받으면 주기를 넘긴다.
        # 여기까지 온 건 이번 주기의 신규분뿐이라 요청 수가 감당 가능한 수준으로 떨어진다.
        try:
            new_news = _refine_with_body(new_news, topic_key, profile)
        except Exception as e:  # noqa: BLE001 — 본문 단계는 절대 수집을 막지 않는다
            logger.error(f"[{topic_key}] 본문 재판정 오류, 스니펫 판정 사용: {e}")

        # 판정에서 탈락한 것도 '이미 본 기사'로 기록해 다음 주기에 본문을 또 받지 않게 한다.
        for n in results:
            collected_this_cycle.add(n.get("url"))

        if not new_news:
            logger.info(f"[{topic_key}] '{keyword}' 본문 재판정 후 남은 뉴스 없음")
            continue

        # 캐시 기록은 여기서 하지 않는다 — 실제 전송에 성공한 뒤 _send_topic 에서 남긴다.
        # (수집 시점에 '전송됨'으로 찍으면 전송 실패분이 영영 재시도되지 않는다)
        pending.add(new_news, topic_key)

        logger.info(f"[{topic_key}] '{keyword}' {len(new_news)}건 수집")

    logger.info(f"[{topic_key}] 뉴스 수집 완료")


def _send_topic(topic_key: str, profile: dict):
    """단일 토픽 전송: 대기 큐 → 해당 토픽 텔레그램 채널.

    큐를 미리 비우지 않고(peek), 전송에 성공한 건만 제거한다. 예전에는 pop_all 로
    먼저 비웠기 때문에 전송 도중 프로세스가 죽으면 그 배치가 통째로 사라졌다.
    캐시 기록(mark_all_sent)도 여기서 — 수집 시점이 아니라 실제로 보낸 뒤에 한다.
    """
    news_list = pending.peek_all(topic_key)
    if not news_list:
        logger.info(f"[{topic_key}] 전송할 뉴스 없음")
        return

    logger.info(f"[{topic_key}] 텔레그램 전송 시작: {len(news_list)}건")
    sent_items = []
    try:
        for news in news_list:
            if telegram.send_news(news, profile["bot_token"], profile["chat_ids"]):
                sent_items.append(news)
                # 통합 포스팅 앱에도 같이 적는다. 실패해도 여기서 멈추지 않는다(unified.push 는 예외를 올리지 않는다).
                unified.push(news, topic_key)
    finally:
        # 도중에 예외가 나도, 이미 보낸 것까지는 큐에서 빼고 캐시에 남긴다.
        # (안 그러면 다음 주기에 같은 기사를 다시 보낸다)
        if sent_items:
            cache.mark_all_sent(sent_items, topic_key)
            pending.remove_sent([n.get("url") for n in sent_items], topic_key)
    logger.info(f"[{topic_key}] 텔레그램 전송 완료: {len(sent_items)}/{len(news_list)}건")


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

    # 한 주기를 끝냈다는 표시. 다음 기동 때 이 시각과 비교해 정지 구간을 알아낸다.
    heartbeat.touch()


def report_downtime(threshold_minutes: int = 90) -> None:
    """직전 주기 이후 공백이 크면 각 토픽 채널에 알린다 (기동 직후 1회).

    봇이 멈춰 있는 동안에는 알릴 수 없으니, 살아난 뒤에 "그동안 비어 있었다"를 알린다.
    네이버는 키워드당 최신 30건만 주므로 긴 공백은 그대로 기사 유실이다.
    """
    gap = heartbeat.gap_minutes()
    if gap is None or gap < threshold_minutes:
        return
    hours = gap / 60
    msg = (f"수집이 {hours:.1f}시간 동안 중단됐다가 재개됐습니다.\n"
           f"그 사이 기사는 네이버가 최신 30건만 주기 때문에 상당수 유실됐을 수 있습니다.")
    logger.error(f"[가동중단] 직전 주기 이후 {hours:.1f}시간 공백 감지")
    for topic_key, profile in active_topics().items():
        _alert(topic_key, profile, msg)


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

    main.py 가 주 1회 스케줄로 건다.
    """
    text = commands.help_message()
    for topic_key, profile in active_topics().items():
        try:
            # 반환값을 봐야 한다 — 예전에는 실패해도 '전송 완료'로 찍혔다.
            if telegram.send_promo(profile["bot_token"], profile["chat_ids"], [text]):
                logger.info(f"[{topic_key}] 명령 안내 전송 완료")
            else:
                logger.warning(f"[{topic_key}] 명령 안내 전송 실패")
        except Exception as e:
            logger.error(f"[안내:{topic_key}] 처리 중 오류: {e}")
