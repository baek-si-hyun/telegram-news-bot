import os
import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

# requests 세션 (connection pooling)
_session = requests.Session()

MAX_MESSAGE_LENGTH = 4096
# sendPhoto 캡션 길이 제한 (sendMessage 와 다름)
MAX_CAPTION_LENGTH = 1024

# 채팅방별 전송 간격 (분당 20메시지 제한 방지)
SEND_DELAY = 1.0

# 국토부 보도자료용 로고 카드.
# 국토부 상세페이지에는 og:image 가 없어 링크 프리뷰 썸네일이 안 뜬다.
# 그래서 네이버 뉴스(썸네일 있음) 사이에서 보도자료가 묻히므로, 사진으로 붙여 눈에 띄게 한다.
CARD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "molit_card.png")

# 같은 사진을 매번 업로드하지 않도록 file_id 캐시 (봇 토큰별, 프로세스 내에서만)
_photo_file_ids: dict[str, str] = {}


def _api_url(bot_token: str) -> str:
    return f"https://api.telegram.org/bot{bot_token}"


# requests 예외 메시지에는 요청 URL 이 그대로 들어가고, 그 URL 에는 /bot<토큰>/ 이 박혀 있다.
# 실제로 logs/bot.log.2 에 현역 봇 토큰이 평문으로 남아 있었다. 로그로 나가기 전에 가린다.
_TOKEN_IN_URL = re.compile(r"(/bot)(\d+:[A-Za-z0-9_-]+)")


def mask_token(text) -> str:
    """로그 문자열에서 텔레그램 봇 토큰을 가린다."""
    return _TOKEN_IN_URL.sub(r"\1***", str(text))


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _prefix(news: dict) -> str:
    """소스별 말머리. 국토부 보도자료는 카테고리까지 표기."""
    if news.get("source") == "molit":
        category = news.get("category")
        if category:
            return f"📢 [국토부·{_escape_html(category)}] "
        return "📢 [국토부] "
    return "📰 "


def _build_message(news: dict) -> str:
    title = _escape_html(news.get("title", "(제목 없음)"))
    url = _escape_html(news.get("url", ""))
    return f'{_prefix(news)}<a href="{url}">{title}</a>'


def _send_to_chat(bot_token: str, chat_id: str, text: str, retry: int = 3) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False},
    }

    rate_limit_retries = 0
    for attempt in range(1, retry + 1):
        try:
            response = _session.post(
                f"{_api_url(bot_token)}/sendMessage",
                json=payload,
                timeout=10,
            )
            if response.status_code == 200:
                return True
            elif response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"[Telegram] Rate limit, {retry_after}초 대기")
                time.sleep(retry_after)
                rate_limit_retries += 1
                if rate_limit_retries <= 3:
                    continue
            else:
                logger.error(f"[Telegram] 전송 실패 chat={chat_id} {response.status_code}: {response.text}")
                return False
        except requests.RequestException as e:
            logger.error(f"[Telegram] 요청 오류 chat={chat_id} 시도 {attempt}/{retry}: {mask_token(e)}")
            if attempt < retry:
                time.sleep(2 ** attempt)

    return False


def _send_request(bot_token: str, chat_ids: list[str], text: str) -> bool:
    """지정된 모든 채팅방에 메시지 전송"""
    success = False
    for chat_id in chat_ids:
        if _send_to_chat(bot_token, chat_id, text):
            success = True
    return success


def _build_caption(news: dict) -> str:
    """사진 캡션 (sendMessage 본문보다 짧은 1024자 제한)"""
    tag = _prefix(news).strip()
    url = _escape_html(news.get("url", ""))
    raw_title = news.get("title", "(제목 없음)")

    caption = f'{tag}\n<a href="{url}">{_escape_html(raw_title)}</a>'
    if len(caption) > MAX_CAPTION_LENGTH:
        overhead = len(f'{tag}\n<a href="{url}">...</a>') + 10
        cut = _escape_html(raw_title[:max(0, MAX_CAPTION_LENGTH - overhead)]) + "..."
        caption = f'{tag}\n<a href="{url}">{cut}</a>'
    return caption


def _send_photo_to_chat(bot_token: str, chat_id: str, caption: str, retry: int = 3) -> bool:
    """로고 카드 사진 + 캡션 전송.

    첫 전송은 파일 업로드, 이후에는 텔레그램이 돌려준 file_id 를 재사용한다.
    file_id 가 만료/무효면(400) 캐시를 버리고 업로드로 재시도한다.
    """
    url = f"{_api_url(bot_token)}/sendPhoto"

    for attempt in range(1, retry + 1):
        cached = _photo_file_ids.get(bot_token)
        try:
            if cached:
                response = _session.post(
                    url,
                    json={"chat_id": chat_id, "photo": cached,
                          "caption": caption, "parse_mode": "HTML"},
                    timeout=15,
                )
            else:
                with open(CARD_PATH, "rb") as f:
                    response = _session.post(
                        url,
                        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                        files={"photo": ("molit_card.png", f, "image/png")},
                        timeout=30,
                    )

            if response.status_code == 200:
                if not cached:
                    photos = (response.json().get("result") or {}).get("photo") or []
                    if photos:
                        _photo_file_ids[bot_token] = photos[-1]["file_id"]
                return True

            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"[Telegram] Rate limit(사진), {retry_after}초 대기")
                time.sleep(retry_after)
                continue

            if response.status_code == 400 and cached:
                # 캐시된 file_id 가 무효 → 버리고 업로드로 재시도
                logger.warning("[Telegram] file_id 무효, 재업로드")
                _photo_file_ids.pop(bot_token, None)
                continue

            logger.error(
                f"[Telegram] 사진 전송 실패 chat={chat_id} {response.status_code}: {response.text[:200]}"
            )
            return False

        except (requests.RequestException, OSError) as e:
            logger.error(f"[Telegram] 사진 요청 오류 chat={chat_id} 시도 {attempt}/{retry}: {mask_token(e)}")
            if attempt < retry:
                time.sleep(2 ** attempt)

    return False


def _send_photo_request(bot_token: str, chat_ids: list[str], caption: str) -> bool:
    success = False
    for chat_id in chat_ids:
        if _send_photo_to_chat(bot_token, chat_id, caption):
            success = True
    return success


def send_news(news: dict, bot_token: str, chat_ids: list[str]) -> bool:
    """뉴스 1건을 (해당 토픽의) 텔레그램 채널로 전송.

    국토부 보도자료는 로고 카드 사진 + 캡션으로 보낸다(네이버 뉴스 썸네일 사이에서 묻히지 않게).
    카드 이미지가 없으면 일반 텍스트 전송으로 자동 폴백한다.
    """
    if news.get("source") == "molit" and os.path.exists(CARD_PATH):
        success = _send_photo_request(bot_token, chat_ids, _build_caption(news))
    else:
        message = _build_message(news)
        if len(message) > MAX_MESSAGE_LENGTH:
            prefix = _prefix(news)
            url = _escape_html(news.get("url", ""))
            max_title_len = MAX_MESSAGE_LENGTH - len(f'{prefix}<a href="{url}">...</a>') - 10
            title = _escape_html(news.get("title", "")[:max(0, max_title_len)]) + "..."
            message = f'{prefix}<a href="{url}">{title}</a>'
        success = _send_request(bot_token, chat_ids, message)

    if success:
        logger.info(f"[Telegram] 전송 완료: {news.get('title', '')[:40]}")
    time.sleep(SEND_DELAY)
    return success


def send_promo(bot_token: str, chat_ids: list[str], messages: list[str]) -> bool:
    """홍보 메시지(여러 건) 전송"""
    success = False
    for i, msg in enumerate(messages):
        if _send_request(bot_token, chat_ids, msg):
            success = True
        if i < len(messages) - 1:
            time.sleep(1)
    return success


def send_alert(text: str, bot_token: str, chat_ids: list[str]) -> bool:
    """오류 알림 등 시스템 메시지 전송"""
    message = f"⚠️ <b>[봇 알림]</b>\n{_escape_html(text)}"
    return _send_request(bot_token, chat_ids, message)
