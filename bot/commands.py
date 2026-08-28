"""텔레그램 키워드 관리 명령 (/add, /del, /list)

기존 봇은 전송(sendMessage)만 했다. 이 모듈은 getUpdates 롱폴링으로 명령을 '수신'해서
관리자(TELEGRAM_ADMIN_IDS)가 검색 키워드를 실시간으로 넣고 뺄 수 있게 한다.
변경분은 storage/keywords.py 에 저장되고 다음 수집 주기(≤30분)부터 반영된다.

동작:
  - active_topics() 의 봇 토큰별로 데몬 스레드 1개가 롱폴링한다.
  - 한 봇이 여러 토픽을 담당하면(공용 토큰) 명령 앞에 토픽명을 붙여야 한다: `/add sto STO`
  - 관리자가 아닌 사용자의 명령은 거부한다. TELEGRAM_ADMIN_IDS 미설정 시 폴링 자체를 끈다.

명령 문법:
  /list  [토픽]                     현재 키워드 목록
  /add   [토픽] <키워드> [검색어…] [가중치]
  /del   [토픽] <키워드>
"""

import json
import logging
import os
import tempfile
import threading
import time

import requests

from config import ADMIN_IDS, TOPICS, active_topics
from storage import keywords as kwstore

logger = logging.getLogger(__name__)

_session = requests.Session()

STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage")
OFFSET_FILE = os.path.join(STORAGE_DIR, "updates_offset.json")

_KNOWN_CMDS = {"/add", "/del", "/list", "/help", "/start"}

# getUpdates 롱폴링 대기(초) — 서버가 이 시간까지 새 업데이트를 기다렸다 응답
_POLL_TIMEOUT = 25

_offset_lock = threading.Lock()


# ──────────────────────────────────────────────
# 오프셋 영속화 (재시작 시 이미 처리한 명령 재실행 방지)
# ──────────────────────────────────────────────
def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_offset(token: str) -> int:
    with _offset_lock:
        return int(_read_json(OFFSET_FILE).get(token, 0))


def _save_offset(token: str, offset: int) -> None:
    with _offset_lock:
        data = _read_json(OFFSET_FILE)
        data[token] = offset
        try:
            _write_json_atomic(OFFSET_FILE, data)
        except OSError as e:
            logger.error(f"[명령] 오프셋 저장 실패: {e}")


# ──────────────────────────────────────────────
# 텔레그램 I/O
# ──────────────────────────────────────────────
def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _reply(token: str, chat_id, text: str) -> None:
    """명령 응답(평문). HTML 파싱을 쓰지 않아 특수문자 이스케이프 불필요."""
    try:
        _session.post(
            _api(token, "sendMessage"),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning(f"[명령] 응답 전송 실패 chat={chat_id}: {e}")


def _get_updates(token: str, offset: int) -> list[dict]:
    try:
        resp = _session.get(
            _api(token, "getUpdates"),
            params={
                "offset": offset,
                "timeout": _POLL_TIMEOUT,
                # message=그룹/DM, channel_post=채널에 올린 명령
                "allowed_updates": json.dumps(["message", "channel_post"]),
            },
            timeout=_POLL_TIMEOUT + 10,
        )
        if resp.status_code != 200:
            logger.error(f"[명령] getUpdates {resp.status_code}: {resp.text[:200]}")
            time.sleep(3)
            return []
        return resp.json().get("result", [])
    except requests.RequestException as e:
        logger.warning(f"[명령] getUpdates 오류: {e}")
        time.sleep(3)
        return []


def _is_chat_admin(token: str, chat_id, user_id) -> bool:
    """user_id 가 chat_id 방의 관리자(creator/administrator)인지 텔레그램에 조회."""
    if chat_id is None or user_id is None:
        return False
    try:
        resp = _session.get(
            _api(token, "getChatMember"),
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.info(f"[명령] getChatMember {resp.status_code}: {resp.text[:120]}")
            return False
        status = (resp.json().get("result") or {}).get("status")
        return status in ("creator", "administrator")
    except requests.RequestException as e:
        logger.warning(f"[명령] getChatMember 오류: {e}")
        return False


# ──────────────────────────────────────────────
# 명령 처리
# ──────────────────────────────────────────────
def help_message() -> str:
    """채널에 주기적으로 보내는 '명령 사용법' 안내.

    telegram.send_promo 가 HTML 파싱을 쓰므로 '<', '>', '&' 는 넣지 않는다.
    """
    return (
        "📌 키워드 관리 명령 안내\n\n"
        "이 방의 관리자는 아래 명령으로 뉴스 검색 키워드를 직접 넣고 뺄 수 있어요.\n\n"
        "• /list — 현재 등록된 키워드 목록\n"
        "• /add 키워드 — 키워드 추가 (띄어쓰기 그대로, 예: /add 청년 임대주택)\n"
        "• /del 키워드 — 키워드 삭제\n\n"
        "여러 개를 한 번에 넣거나 뺄 땐 | 로 구분하세요.\n"
        "예: /add 구리트램 | 워커힐 재건축 | 양정역세권\n\n"
        "변경은 다음 수집(최대 30분)부터 반영됩니다."
    )


def _usage(topic_keys: list[str]) -> str:
    text = help_message()
    if len(topic_keys) > 1:
        text += f"\n(방이 자동 인식 안 될 땐 앞에 토픽명: 예) /add {topic_keys[0]} 키워드)"
    return text


def _topic_by_chat(chat_id, topic_keys: list[str]):
    """명령이 올라온 채팅방 id 로 어느 토픽인지 자동 판별. 매칭 없으면 None.

    각 토픽은 config 에 자기 chat_ids(문자열 목록)를 가진다. STO 방/부동산 방이
    서로 다르므로, 방 id 만으로 토픽을 구분할 수 있다(명령에 토픽명 불필요).
    """
    if chat_id is None:
        return None
    cid = str(chat_id)
    for tk in topic_keys:
        if cid in TOPICS.get(tk, {}).get("chat_ids", []):
            return tk
    return None


def _cmd_list(token: str, chat_id, topic: str) -> None:
    added, disabled = kwstore.snapshot(topic)
    config_kw = list(TOPICS[topic]["keywords"].keys())

    lines = [f"📋 [{topic}] 키워드"]

    lines.append(f"\n■ 추가된 키워드 ({len(added)}개) — /del 로 삭제 가능")
    if added:
        for kw, meta in added.items():
            lines.append(f"  • {kw} (검색:'{meta.get('query', kw)}', 가중치 {meta.get('weight')})")
    else:
        lines.append("  (없음)")

    active_config = [k for k in config_kw if k not in disabled]
    lines.append(f"\n■ 기본 키워드 ({len(active_config)}/{len(config_kw)} 활성)")
    lines.append("  " + ", ".join(active_config) if active_config else "  (없음)")

    if disabled:
        lines.append(f"\n■ 비활성 처리된 기본 키워드 ({len(disabled)}개) — /add 로 되살림")
        lines.append("  " + ", ".join(disabled))

    _reply(token, chat_id, "\n".join(lines))


_ADD_USAGE = (
    "사용법: /add 키워드\n"
    "예) /add 청년 임대주택\n"
    "여러 개는 | 로 구분: /add 구리트램 | 워커힐 재건축 | 양정역세권"
)
_DEL_USAGE = (
    "사용법: /del 키워드\n"
    "예) /del 청년 임대주택\n"
    "여러 개는 | 로 구분: /del 구리트램 | 워커힐 재건축"
)


def _split_keywords(args: list[str]) -> list[str]:
    """'|' 로 구분된 여러 키워드를 파싱.

    각 조각의 앞뒤 공백은 제거하되 내부 띄어쓰기는 보존한다('워커힐 재건축').
    빈 조각과 중복은 버리고, 입력 순서는 유지한다.
    """
    raw = " ".join(args).strip()
    result: list[str] = []
    seen: set[str] = set()
    for part in raw.split("|"):
        kw = part.strip()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result


def _cmd_add(token: str, chat_id, topic: str, args: list[str]) -> None:
    keywords = _split_keywords(args)
    if not keywords:
        _reply(token, chat_id, _ADD_USAGE)
        return

    weight = kwstore.default_weight(TOPICS[topic])
    for kw in keywords:
        kwstore.add(topic, kw, kw, weight)

    if len(keywords) == 1:
        body = f"✅ [{topic}] '{keywords[0]}' 추가 완료."
    else:
        listed = "\n".join(f"  • {k}" for k in keywords)
        body = f"✅ [{topic}] 키워드 {len(keywords)}개 추가 완료.\n{listed}"
    _reply(token, chat_id, body + "\n다음 수집(최대 30분)부터 반영됩니다.")


def _cmd_del(token: str, chat_id, topic: str, args: list[str]) -> None:
    keywords = _split_keywords(args)
    if not keywords:
        _reply(token, chat_id, _DEL_USAGE)
        return

    config_kw = set(TOPICS[topic]["keywords"].keys())
    buckets: dict[str, list[str]] = {
        "removed": [], "disabled": [], "already_disabled": [], "not_found": [],
    }
    for kw in keywords:
        buckets[kwstore.remove(topic, kw, config_kw)].append(kw)

    lines = [f"[{topic}] /del 결과"]
    if buckets["removed"]:
        lines.append("🗑 삭제: " + ", ".join(buckets["removed"]))
    if buckets["disabled"]:
        lines.append("🚫 기본 키워드 비활성(코드는 그대로, /add 로 되살림): " + ", ".join(buckets["disabled"]))
    if buckets["already_disabled"]:
        lines.append("ℹ️ 이미 비활성: " + ", ".join(buckets["already_disabled"]))
    if buckets["not_found"]:
        lines.append("❓ 못 찾음(/list 확인): " + ", ".join(buckets["not_found"]))
    if buckets["removed"] or buckets["disabled"]:
        lines.append("다음 수집부터 반영됩니다.")
    _reply(token, chat_id, "\n".join(lines))


def _handle_update(token: str, topic_keys: list[str], upd: dict) -> None:
    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower()  # /add@MyBot → /add
    if cmd not in _KNOWN_CMDS:
        return  # 봇과 무관한 슬래시 입력은 조용히 무시

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    args = parts[1:]

    topic_by_chat = _topic_by_chat(chat_id, topic_keys)
    user = msg.get("from")
    is_super = user is not None and str(user.get("id", "")) in ADMIN_IDS

    # ── 권한 판정 ────────────────────────────────────────
    # 원칙: '그 방(우리 토픽 채널/그룹)의 관리자'면 누구나 사용 가능.
    #   · 채널 게시물(from 없음): 채널은 관리자만 글을 올리므로 우리 채널이면 통과.
    #   · 그룹 메시지: getChatMember 로 보낸 사람이 그 방 관리자인지 실시간 확인.
    #   · (옵션) TELEGRAM_ADMIN_IDS 슈퍼관리자: 어디서든(DM 포함) 통과.
    if user is None:
        authorized = topic_by_chat is not None
    elif is_super:
        authorized = True
    elif topic_by_chat is not None and _is_chat_admin(token, chat_id, user.get("id")):
        authorized = True
    else:
        authorized = False

    if not authorized:
        # 우리 토픽 방에서 비관리자가 시도한 경우에만 안내(그 외엔 조용히 무시)
        if user is not None and topic_by_chat is not None:
            _reply(token, chat_id, "⛔ 이 방의 관리자만 키워드를 바꿀 수 있습니다.")
            logger.info(f"[명령] 권한 거부 user={user.get('id')} chat={chat_id} cmd={cmd}")
        return

    if cmd in ("/help", "/start"):
        _reply(token, chat_id, _usage(topic_keys))
        return

    # ── 토픽 결정 ────────────────────────────────────────
    # 방 관리자/채널로 승인된 경우: 그 방의 토픽으로 고정(다른 토픽 오염 방지).
    # 슈퍼관리자(DM 등): ①명시적 접두어 → ②채팅방 → ③단일토픽 폴백.
    if is_super:
        topic = None
        if args and args[0] in topic_keys:
            topic = args[0]
            args = args[1:]
        if topic is None:
            topic = topic_by_chat
        if topic is None and len(topic_keys) == 1:
            topic = topic_keys[0]
        if topic is None:
            _reply(
                token, chat_id,
                "어느 토픽인지 알 수 없습니다. 해당 토픽 채팅방에서 입력하거나 "
                f"앞에 토픽명을 붙이세요: {', '.join(topic_keys)}\n예) {cmd} {topic_keys[0]} ...",
            )
            return
    else:
        topic = topic_by_chat  # 승인 조건상 None 이 아님

    if cmd == "/list":
        _cmd_list(token, chat_id, topic)
    elif cmd == "/add":
        _cmd_add(token, chat_id, topic, args)
    elif cmd == "/del":
        _cmd_del(token, chat_id, topic, args)


# ──────────────────────────────────────────────
# 폴링 루프
# ──────────────────────────────────────────────
def _poll_loop(token: str, topic_keys: list[str], is_running) -> None:
    offset = _load_offset(token)
    logger.info(f"[명령] 폴링 시작 topics={topic_keys} (offset={offset})")
    while is_running():
        updates = _get_updates(token, offset)
        if not updates:
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                _handle_update(token, topic_keys, upd)
            except Exception as e:
                logger.error(f"[명령] 업데이트 처리 오류: {e}")
        _save_offset(token, offset)
    logger.info(f"[명령] 폴링 종료 topics={topic_keys}")


def start_polling(is_running) -> list[threading.Thread]:
    """활성 토픽의 봇 토큰별로 명령 폴링 데몬 스레드를 시작한다.

    is_running: 종료 여부를 반환하는 콜러블(main 의 _running 플래그).
    반환: 시작된 스레드 목록(테스트/디버깅용).
    """
    token_map: dict[str, list[str]] = {}
    for key, profile in active_topics().items():
        token_map.setdefault(profile["bot_token"], []).append(key)

    if not token_map:
        logger.info("키워드 명령: 활성 토픽이 없어 폴링을 시작하지 않음")
        return []

    threads = []
    for tok, keys in token_map.items():
        t = threading.Thread(
            target=_poll_loop, args=(tok, keys, is_running),
            daemon=True, name=f"cmd-{'-'.join(keys)}",
        )
        t.start()
        threads.append(t)

    extra = f" / 슈퍼관리자 {len(ADMIN_IDS)}명" if ADMIN_IDS else ""
    logger.info(f"키워드 명령 폴링 시작: 봇 {len(threads)}개 (각 방 관리자면 사용 가능){extra}")
    return threads
