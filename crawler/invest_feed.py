"""오늘장(invest-backend) 부동산 뉴스 피드 — 봇은 받아서 거르고 보내기만 한다.

명세: C:/workspace/_wt/_specs/realestate-news-bridge.md (8장).

오늘장이 매시 :25·:55 에 부동산 키워드 28개(네이버)와 국토부 보도자료를 모아 두고, 봇은 :00·:30
주기에 도커 전용 내부망으로 그 원본을 읽는다. 거르기(제휴사 게이트·모델·본문 재판정·중복 제거)와
전송 이력은 지금처럼 봇이 한다 — 입력 출처만 바뀐다.

## 원천 스위치 (config.REALESTATE_SOURCE)

- direct  지금과 같다. 피드를 부르지 않는다. 토큰 파일이 있을 때만 전송분 ack 를 보낸다.
- shadow  피드를 받아 쿼리별 원본 건수·URL 겹침만 로그로 비교한다. 판정·전송은 direct 로 하고,
          피드 쪽은 아무것도 쓰지 않는다(history·sent·pending·seen·커서·ack 전부).
- invest  피드를 원본으로 쓴다.
          · 피드 실패·401·403·503·타임아웃 → 그 주기는 토픽 전체 direct, 커서 그대로
          · as_of 가 MAX_AGE_MIN(70분)보다 오래된 소스 → 그 소스만 direct
          · 봇 기본 키워드의 쿼리가 피드 목록에 없으면 → 그 키워드만 direct
          · 토큰 파일이 없으면 → direct

## 지켜야 할 것

- **텔레그램 알림(_alert)을 부르지 않는다.** 실패·낡음·전환은 bot.log 에 `[invest-feed]` 로만,
  상태가 바뀔 때 1회 + 지속되면 1시간에 1회.
- 이 모듈의 실패가 수집·전송을 막지 않는다 — 무슨 일이 있어도 예외를 올리지 않고 direct 로 돌아간다.
- 토큰 값은 어디에도 찍지 않는다. 요청에 X-Forwarded-For 를 넣지 않고, 환경의 프록시도 타지 않는다.
- 커서 파일은 원자적으로 쓴다(임시 파일 → os.replace). 지우면 최근 2시간부터 다시 받는다 —
  중복은 sent 캐시가 막는다. 커서는 네이버 hit 에만 쓴다. 국토부는 커서 없이 매번 최근분을 받는다
  (item_id 가 담당부서 조회 전에 매겨져, 조회가 늦게 성공한 행을 id 커서가 건너뛰기 때문 — load_cursor).
- 피드 행은 형식을 검사해 받는다(문자열 아닌 값·http(s) 아닌 링크·따옴표 든 링크는 버림).
"""

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from config import INVEST_NEWS_URL, REALESTATE_SOURCE

logger = logging.getLogger(__name__)

# 피드를 쓰는 토픽은 부동산 하나뿐이다. STO 는 바뀌는 것이 없다.
FEED_TOPIC = "realestate"
MODES = ("direct", "shadow", "invest")

# 두 compose 가 같은 파일을 secrets 로 마운트한다(저장소 밖 C:/workspace/_secrets). 매번 다시 읽는다 —
# 파일을 바꾸면 재기동 없이 다음 주기부터 새 값을 쓴다.
TOKEN_PATH = "/run/secrets/news_bridge_token"

FEED_PATH = "/api/internal/realestate-news"
ACK_PATH = "/api/internal/realestate-news/sent"
TIMEOUT = (3, 10)            # (연결, 읽기) 초
PAGE_LIMIT = 500             # 네이버 hit 기준 한 쪽 크기 (오늘장 최대 1000)
MAX_PAGES = 5                # has_more 여도 한 주기에 최대 5쪽
MAX_AGE_MIN = 70             # as_of 가 이보다 오래되면 그 소스는 낡음 (오늘장 건너뛰는 창 최장 2회를 견디는 값)
FUTURE_SKEW_MIN = 5          # as_of 가 이보다 더 미래면 시계·데이터 오류로 보고 낡음 처리
CURSOR_MAX_AGE_HOURS = 6     # 커서가 이보다 오래 안 넘어갔으면(긴 장애·direct 지속) 버리고 '최근 2시간'부터 —
                             # 밀린 수천 건의 옛 기사가 복구 직후 한꺼번에 나가지 않게 한다
ACK_BATCH = 200              # ack 한 번에 보낼 URL 수 상한 (오늘장 입력 상한)
ACK_URL_MAX = 2048           # URL 한 개 길이 상한 (오늘장 입력 상한)
NOTICE_INTERVAL_SEC = 3600   # 같은 문제가 이어지면 이 간격으로만 다시 기록

_UA = "telegram-news-bot/1.0 (+https://github.com/baek-si-hyun/telegram-news-bot)"

_STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage")


# ──────────────────────────────────────────────
# 상태 로그 — 바뀔 때 1회, 이어지면 1시간에 1회
# ──────────────────────────────────────────────
_notices: dict[str, dict] = {}


def _notice(key: str, active: bool, message: str = "", recovered: str = "") -> None:
    """문제 상태(key)를 기록한다. active=True 면 문제가 있는 것.

    · 문제가 새로 생기거나 내용(message)이 바뀌면 바로 WARNING 1줄
    · 같은 문제가 이어지면 NOTICE_INTERVAL_SEC 마다 1줄
    · 문제가 사라지면 recovered 를 INFO 1줄 (주어졌을 때만)
    """
    now = time.monotonic()
    prev = _notices.get(key)
    if active:
        if prev is None or prev["message"] != message:
            logger.warning(f"[invest-feed] {message}")
            _notices[key] = {"message": message, "since": now, "last": now}
        elif now - prev["last"] >= NOTICE_INTERVAL_SEC:
            minutes = int((now - prev["since"]) // 60)
            logger.warning(f"[invest-feed] {message} ({minutes}분째 계속)")
            prev["last"] = now
        return
    if prev is not None:
        del _notices[key]
        if recovered:
            logger.info(f"[invest-feed] {recovered}")


_mode_logged = False


def mode() -> str:
    """설정된 원천 모드. 모르는 값이면 direct (1회 경고)."""
    global _mode_logged
    m = REALESTATE_SOURCE if REALESTATE_SOURCE in MODES else "direct"
    if not _mode_logged:
        _mode_logged = True
        if m != REALESTATE_SOURCE:
            logger.warning(f"[invest-feed] REALESTATE_SOURCE={REALESTATE_SOURCE!r} 는 모르는 값 — direct 로 동작")
        elif m != "direct":
            logger.info(f"[invest-feed] 부동산 수집 원천: {m}")
    return m


# ──────────────────────────────────────────────
# 토큰 · 커서
# ──────────────────────────────────────────────
def _read_token() -> str | None:
    """토큰 파일 값. 없거나 비었거나 못 읽으면 None. 값은 어디에도 찍지 않는다."""
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        return None
    return tok or None


def has_token() -> bool:
    return _read_token() is not None


def _cursor_path(topic_key: str) -> str:
    return os.path.join(_STORAGE_DIR, topic_key, "invest_cursor.json")


def _valid_cursor(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def load_cursor(topic_key: str) -> int | None:
    """네이버 hit 커서. 파일이 없거나 깨졌거나 CURSOR_MAX_AGE_HOURS 보다 오래됐으면 None(최근 2시간부터).

    국토부는 커서를 쓰지 않는다 — item_id 는 오늘장이 처음 본 순간(담당부서 조회 전) 매겨지므로,
    앞 번호 행의 담당부서 조회가 늦게 성공하면 id 커서를 이미 지나쳐 영영 안 온다. 국토부는 매번
    오늘장 기본 창(최근분)을 받고, 중복은 molit seen(URL 완전일치, 30일)이 막는다.
    """
    try:
        with open(_cursor_path(topic_key), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _valid_cursor(data.get("cursor")):
        return None
    try:
        updated = datetime.fromisoformat(str(data.get("updated_at")))
    except ValueError:
        return None
    if (datetime.now() - updated).total_seconds() > CURSOR_MAX_AGE_HOURS * 3600:
        _notice("cursor_old", True, f"커서가 {CURSOR_MAX_AGE_HOURS}시간 넘게 안 넘어갔다 — 버리고 최근 2시간분부터 받는다")
        return None
    _notice("cursor_old", False)
    return data["cursor"]


def save_cursor(topic_key: str, cursor: int | None) -> bool:
    """네이버 커서를 원자적으로 쓴다(임시 파일 → os.replace). 실패해도 예외를 올리지 않는다."""
    if cursor is None:
        return False
    data = {"cursor": cursor, "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    path = _cursor_path(topic_key)
    try:
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
    except Exception as e:  # noqa: BLE001 — 커서 저장 실패는 다음 주기 중복 수신일 뿐(sent 캐시가 막는다)
        logger.error(f"[invest-feed] 커서 저장 실패: {type(e).__name__}")
        return False
    return True


# ──────────────────────────────────────────────
# 피드
# ──────────────────────────────────────────────
@dataclass
class Feed:
    queries: set = field(default_factory=set)            # 오늘장이 수집하는 쿼리 문자열
    naver_by_query: dict = field(default_factory=dict)   # 쿼리 → 네이버 원본 items (title·description·originallink·link·pubDate)
    molit: list = field(default_factory=list)            # 담당부서 조회까지 끝난 보도자료
    naver_fresh: bool = False                            # as_of.naver 가 MAX_AGE_MIN 안
    molit_fresh: bool = False
    next_cursor: int | None = None
    pages: int = 0
    dropped: int = 0                                     # 형식이 틀려 버린 행 수


class FeedError(Exception):
    """피드를 쓸 수 없음 — 이번 주기는 토픽 전체 direct."""


def _session() -> requests.Session:
    s = requests.Session()
    # 환경의 프록시(HTTP_PROXY 등)를 타지 않는다 — 내부망으로 곧장 간다. 프록시가 붙이는
    # X-Forwarded-For 같은 헤더도 생기지 않는다.
    s.trust_env = False
    return s


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": _UA}


def _age_minutes(iso: str | None) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


def _fresh(age: float | None) -> bool:
    return age is not None and -FUTURE_SKEW_MIN <= age <= MAX_AGE_MIN


_NAVER_TEXT_FIELDS = ("title", "description", "originallink", "link", "pubDate")
_BAD_URL_CHARS = set('"<>\\ \t\r\n')


def _ok_url(u) -> bool:
    return (isinstance(u, str) and u.startswith(("http://", "https://")) and len(u) <= ACK_URL_MAX
            and not (set(u) & _BAD_URL_CHARS))


def _clean_naver_hit(it) -> dict | None:
    """네이버 hit 한 행을 naver.search 가 받는 원본 모양으로. 형식이 틀리면 None(버림).

    문자열이 아닌 값이 섞이면 판정·중복 제거 단계에서 예외가 나 토픽 수집 전체가 멈춘다.
    링크는 http(s) 만, 따옴표·꺾쇠·공백이 든 것은 버린다(텔레그램 href 가 깨져 전송이 영구 실패한다).
    """
    if not isinstance(it, dict) or not isinstance(it.get("query"), str):
        return None
    row = {"query": it["query"]}
    for k in _NAVER_TEXT_FIELDS:
        v = it.get(k)
        if v is None:
            v = ""
        if not isinstance(v, str):
            return None
        row[k] = v
    if not row["title"].strip() or not _ok_url(row["originallink"] or row["link"]):
        return None
    if row["originallink"] and not _ok_url(row["originallink"]):
        return None
    return row


def _clean_molit_row(it) -> dict | None:
    if not isinstance(it, dict):
        return None
    url, title, dept = it.get("url"), it.get("title"), it.get("dept")
    if not _ok_url(url) or not isinstance(title, str) or not title.strip() or not isinstance(dept, str):
        return None
    return {"url": url, "title": title, "dept": dept,
            "field": it.get("field") if isinstance(it.get("field"), str) else "",
            "pub_date": it.get("pub_date") if isinstance(it.get("pub_date"), str) else ""}


def _get_page(session: requests.Session, token: str, cursor) -> dict:
    params = {"limit": PAGE_LIMIT}
    if cursor is not None:
        params["cursor"] = cursor
    try:
        res = session.get(f"{INVEST_NEWS_URL}{FEED_PATH}", params=params, headers=_headers(token),
                          timeout=TIMEOUT, allow_redirects=False)
    except requests.Timeout:
        raise FeedError("타임아웃")
    except requests.RequestException as e:
        raise FeedError(f"연결 실패({type(e).__name__})")
    if res.status_code != 200:
        raise FeedError(f"HTTP {res.status_code}")
    try:
        data = res.json()
    except ValueError:
        raise FeedError("응답이 JSON 이 아님")
    if not isinstance(data, dict):
        raise FeedError("응답 형식 오류")
    return data


def _fetch(topic_key: str, token: str, use_cursor: bool) -> Feed:
    """피드를 받는다. 첫 쪽이 실패하면 FeedError(토픽 전체 direct).

    2쪽부터 실패하면 이미 받은 쪽까지만 쓰고 그 쪽의 next_cursor 까지만 넘긴다 — 큰 밀림에서 특정 쪽이
    계속 실패해도 커서가 조금씩은 나아가게.
    """
    cursor = load_cursor(topic_key) if use_cursor else None
    feed = Feed()
    seen_molit = set()
    session = _session()
    try:
        for page in range(MAX_PAGES):
            try:
                data = _get_page(session, token, cursor)
                naver = data.get("naver")
                molit = data.get("molit")
                queries = data.get("queries")
                if not isinstance(naver, list) or not isinstance(molit, list) or not isinstance(queries, list):
                    raise FeedError("응답 형식 오류(naver·molit·queries)")
                nc = data.get("next_cursor")
                if nc is not None and not _valid_cursor(nc):
                    raise FeedError("응답 형식 오류(커서)")
            except FeedError as e:
                if page == 0:
                    raise
                _notice("page", True, f"{page + 1}쪽 실패({e}) — 받은 {page}쪽까지만 쓴다")
                break
            if page == 0:
                _notice("page", False)
                as_of = data.get("as_of") if isinstance(data.get("as_of"), dict) else {}
                feed.naver_fresh = _fresh(_age_minutes(as_of.get("naver")))
                feed.molit_fresh = _fresh(_age_minutes(as_of.get("molit")))
                feed.queries = {q for q in queries if isinstance(q, str)}
                _notice("stale_naver", not feed.naver_fresh,
                        f"네이버 피드 낡음(as_of {str(as_of.get('naver'))[:40]}) — 네이버는 직접 수집",
                        "네이버 피드 정상 — 오늘장 피드 사용")
                _notice("stale_molit", not feed.molit_fresh,
                        f"국토부 피드 낡음(as_of {str(as_of.get('molit'))[:40]}) — 국토부는 직접 수집",
                        "국토부 피드 정상 — 오늘장 피드 사용")
            for it in naver:
                row = _clean_naver_hit(it)
                if row is None:
                    feed.dropped += 1
                    continue
                feed.naver_by_query.setdefault(row["query"], []).append(row)
            for it in molit:
                row = _clean_molit_row(it)
                if row is None:
                    feed.dropped += 1
                    continue
                if row["url"] in seen_molit:
                    continue
                seen_molit.add(row["url"])
                feed.molit.append(row)
            feed.pages = page + 1
            if nc is not None and cursor is not None and nc < cursor:
                # 오늘장 표가 새로 만들어졌거나 번호가 되감겼다 — 서버 값을 따른다(중복은 sent 캐시가 막는다).
                logger.warning(f"[invest-feed] 커서 역행 {cursor} → {nc} — 서버 값을 따른다")
            progressed = nc is not None and nc != cursor
            if nc is not None:
                feed.next_cursor = cursor = nc
            if data.get("has_more") is not True or not progressed:
                break
    finally:
        session.close()
    _notice("dropped", feed.dropped > 0, f"형식이 틀린 피드 행 {feed.dropped}건을 버렸다")
    return feed


@dataclass
class Cycle:
    """한 주기(한 토픽)의 피드 사용 상태. scheduler.run_cycle 이 토픽마다 하나 만든다."""
    topic_key: str
    mode: str = "direct"
    feed: Feed | None = None        # invest 모드에서 쓸 수 있는 피드 (None = 이번 주기 토픽 전체 direct)
    shadow: Feed | None = None      # shadow 모드에서 비교만 할 피드
    ack_enabled: bool = False
    sent_urls: list = field(default_factory=list)


def begin(topic_key: str) -> Cycle | None:
    """토픽 주기 시작. 부동산이 아니면 None(아무것도 안 함). 절대 예외를 올리지 않는다."""
    if topic_key != FEED_TOPIC:
        return None
    m, token = "direct", None
    try:
        m = mode()
        cyc = Cycle(topic_key=topic_key, mode=m)
        token = _read_token()
        if m == "direct":
            # 오늘장 화면은 봇이 보낸 기사만 보여 준다 — direct 로 보낸 것도 표시해 둔다(토큰 있을 때만).
            cyc.ack_enabled = token is not None
            return cyc
        if token is None:
            _notice("token", True, f"토큰 파일 없음({TOKEN_PATH}) — {m} 모드지만 부동산 전체 직접 수집")
            return cyc
        _notice("token", False, recovered="토큰 파일 확인 — 피드 사용")
        if m == "shadow":
            # 커서를 읽지도 쓰지도 않는다 → 매번 '최근 2시간분'과 직접 수집 결과를 비교한다.
            try:
                cyc.shadow = _fetch(topic_key, token, use_cursor=False)
                _notice("feed", False, recovered="피드 복구")
            except FeedError as e:
                _notice("feed", True, f"shadow 피드 실패({e}) — 비교 건너뜀(판정·전송은 원래대로 직접 수집)")
            return cyc
        # invest
        cyc.ack_enabled = True
        try:
            cyc.feed = _fetch(topic_key, token, use_cursor=True)
            _notice("feed", False, recovered="피드 복구 — 오늘장 피드 사용")
        except FeedError as e:
            _notice("feed", True, f"피드 실패({e}) — 이번 주기 부동산 전체 직접 수집")
        return cyc
    except Exception as e:  # noqa: BLE001 — 피드 쪽 어떤 문제도 수집을 막지 않는다
        logger.error(f"[invest-feed] 피드 준비 중 예외 — 직접 수집: {type(e).__name__}")
        # 피드는 못 써도 보낸 기사의 '보냄' 표시는 계속한다(shadow 는 원래 안 함).
        return Cycle(topic_key=topic_key, mode=m, ack_enabled=token is not None and m != "shadow")


def note_mismatch(missing_queries: list[str]) -> None:
    """봇 기본 키워드인데 피드 목록에 없는 쿼리 — 그 키워드만 직접 수집한다."""
    _notice("mismatch", bool(missing_queries),
            f"목록 불일치: {', '.join(sorted(missing_queries))} — 이 쿼리만 직접 수집",
            "목록 불일치 해소")


def finish(cyc: Cycle | None) -> None:
    """주기 끝: 전송 성공분을 오늘장에 '보냄'으로 표시(ack). 실패해도 로그만 남긴다."""
    if cyc is None:
        return
    try:
        if not cyc.ack_enabled or not cyc.sent_urls:
            return
        token = _read_token()
        if token is None:
            logger.debug("[invest-feed] 토큰 파일 없음 — ack 건너뜀")
            return
        urls, seen = [], set()
        for u in cyc.sent_urls:
            if isinstance(u, str) and u and len(u) <= ACK_URL_MAX and u not in seen:
                seen.add(u)
                urls.append(u)
        marked, failed = 0, 0
        session = _session()
        try:
            for i in range(0, len(urls), ACK_BATCH):
                batch = urls[i:i + ACK_BATCH]
                n = _post_ack(session, token, batch)
                if n is None:
                    failed += len(batch)
                else:
                    marked += n
        finally:
            session.close()
        _notice("ack", failed > 0, f"ack 실패 {failed}건 — 오늘장 화면에 이 기사들이 안 뜬다(전송은 정상)",
                "ack 복구")
        logger.debug(f"[invest-feed] ack {len(urls)}건 → marked {marked}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[invest-feed] ack 중 예외: {type(e).__name__}")


def _post_ack(session: requests.Session, token: str, urls: list[str]) -> int | None:
    """ack 한 묶음. 실패하면 1회만 다시 시도. 반환: marked 수 또는 None(실패)."""
    for _ in range(2):
        try:
            res = session.post(f"{INVEST_NEWS_URL}{ACK_PATH}", json={"urls": urls}, headers=_headers(token),
                               timeout=TIMEOUT, allow_redirects=False)
            if res.status_code == 200:
                try:
                    marked = res.json().get("marked")
                except (ValueError, AttributeError):
                    marked = None
                return marked if isinstance(marked, int) else 0
        except requests.RequestException:
            pass
    return None


# ──────────────────────────────────────────────
# shadow 비교 — 로그만, 아무것도 쓰지 않는다
# ──────────────────────────────────────────────
def _item_url(it: dict) -> str:
    return (it.get("originallink") or it.get("link") or "") if isinstance(it, dict) else ""


def shadow_compare_naver(topic_key: str, per_query: dict[str, list[dict]], feed: Feed) -> None:
    """직접 수집 원본(per_query: 쿼리 → 네이버 items)과 피드 원본을 쿼리별로 비교해 로그로만 남긴다.

    피드는 '최근 2시간분'이고 직접 수집은 '쿼리별 최신 30건'이라 같은 집합이 아니다. 그래서
    · 피드에만 있는 URL (직접 30건 밖이거나 오늘장이 더 잡은 것)
    · 직접 수집 중 발행 2시간 안인데 피드에 없는 URL (오늘장이 놓친 것 — 0 이어야 정상)
    을 센다.
    """
    tot_feed = tot_direct = tot_overlap = tot_feed_only = tot_missing = 0
    lines = []
    for query, direct_items in per_query.items():
        f_urls = {_item_url(i) for i in feed.naver_by_query.get(query, [])} - {""}
        d_urls = {_item_url(i) for i in direct_items} - {""}
        recent = {_item_url(i) for i in direct_items if _recent(i.get("pubDate"), 120)} - {""}
        overlap = len(f_urls & d_urls)
        feed_only = len(f_urls - d_urls)
        missing = len(recent - f_urls)
        tot_feed += len(f_urls)
        tot_direct += len(d_urls)
        tot_overlap += overlap
        tot_feed_only += feed_only
        tot_missing += missing
        if missing or feed_only:
            lines.append(f"'{query}' 피드 {len(f_urls)} · 직접 {len(d_urls)} · 겹침 {overlap} · "
                         f"피드에만 {feed_only} · 직접(2시간 안)에만 {missing}")
    logger.info(f"[invest-feed] shadow 네이버 {len(per_query)}쿼리: 피드 {tot_feed} · 직접 {tot_direct} · "
                f"겹침 {tot_overlap} · 피드에만 {tot_feed_only} · 직접(2시간 안)에만 {tot_missing}")
    for line in lines:
        logger.info(f"[invest-feed] shadow 차이 {line}")


def shadow_compare_molit(topic_key: str, feed: Feed, filter_unseen) -> None:
    """피드 보도자료 중 직접 수집이 아직 못 본(molit seen 에 없는) URL 수를 로그로만 남긴다.

    filter_unseen: storage.cache.filter_new_urls 를 넘긴다 — 읽기만 하고 쓰지 않는다.
    직접 수집이 이번 주기에 먼저 돌아 seen 을 갱신했으므로, 정상이라면 0 이다.
    """
    items = [{"url": it.get("url")} for it in feed.molit if isinstance(it, dict) and isinstance(it.get("url"), str)]
    unseen = filter_unseen(items, topic_key, "molit") if items else []
    logger.info(f"[invest-feed] shadow 국토부: 피드 {len(items)}건 · 직접 수집이 못 본 것 {len(unseen)}건")


def _recent(pub_date, minutes: int) -> bool:
    from email.utils import parsedate_to_datetime
    if not isinstance(pub_date, str) or not pub_date:
        return False
    try:
        dt = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() <= minutes * 60
