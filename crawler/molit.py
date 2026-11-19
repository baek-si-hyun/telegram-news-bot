"""국토교통부 보도자료 수집 (네이버 뉴스와 별개 소스)

- 목록: https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp  (서버렌더링 HTML, JS 불필요)
- 상세: dtl.jsp?id=...  →  '담당부서' 추출

※ 접근 주의
  세션 쿠키(TMOSHCooKie)를 보존하지 않으면 서버가 같은 URL로 307을 무한 반복한다.
  requests.Session 이 첫 307 에서 심어 주는 쿠키를 유지하면 정상 200 — 브라우저 UA 위장은
  필요 없다(2026-11-17 봇 UA 로 실측). robots.txt 상 /USR/NEWS/ 허용.
  공식 대체 경로는 없다: 정책브리핑 RSS 는 2026-07-01 종료, data.go.kr 에 국토부 보도자료 API 없음,
  molit 자체 RSS(board_rss.jsp?rss_id=NEWS)는 담당부서가 없고 최근 10건뿐이다.

※ 분류 주의
  국토부 자체 '분야'는 '도로철도'(철도+도로 합침) / '주택토지'(택지+주택 합침) 라서
  철도/도로/택지/주택 4분류가 불가능하다. 게다가 '인천 2호선 연장'이 교통물류로,
  '전세사기피해자'가 일반으로 분류되는 등 분야 자체가 부정확하다.
  → 분야로 거르지 않고, 담당부서(1순위) + 제목 키워드(2순위)로 판별한다.

관련성 점수 필터(crawler/naver.py)는 태우지 않는다. 국토부 공식 발표이고
담당부서로 이미 선별되므로, 점수 필터를 태우면 '[장관동정] 도로 침수 대비' 같은
공식 발표가 임계값에 걸려 잘려나간다.
"""

import logging
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from storage import cache

logger = logging.getLogger(__name__)

LIST_URL = "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp"

# 브라우저로 위장하지 않고 봇임을 밝힌다 (307 루프는 UA 가 아니라 세션 쿠키로 푼다).
_UA = "telegram-news-bot/1.0 (+https://github.com/baek-si-hyun/telegram-news-bot)"

# 목록에서 한 번에 가져올 건수 (30분 주기라 넉넉)
LIST_SIZE = 30
# 상세 페이지 조회 간격 (정부 사이트 예의)
DETAIL_DELAY = 0.5

# 중복 판별 네임스페이스 (네이버 뉴스 캐시와 분리)
SEEN_NS = "molit"

# ── 분류 규칙 ──────────────────────────────────────────
# 1순위: 담당부서명에 아래 단어가 있으면 그 카테고리 (위에서부터 먼저 매칭)
#   '부동산'(부동산개발산업과·부동산제도기획과 등)은 일반 부동산 부서 → 주택으로 귀속
DEPT_RULES = [
    ("철도", ["철도", "광역급행"]),
    ("도로", ["도로"]),
    ("택지", ["택지", "신도시", "토지"]),
    ("주택", ["주택", "임대", "주거", "부동산"]),
]
# 2순위: 부서로 판별 안 되면 제목 키워드
TITLE_RULES = [
    ("철도", ["철도", "전철", "지하철", "GTX", "광역급행", "호선", "복선", "트램", "역세권", "간선급행"]),
    ("도로", ["도로", "고속도로", "국도", "교량", "터널", "휴게소", "나들목", "우회도로"]),
    ("택지", ["택지", "신도시", "지구 지정", "지구지정", "국가산단", "산업단지",
             "토지거래허가", "개발제한구역", "그린벨트", "공공주택지구"]),
    ("주택", ["주택", "아파트", "분양", "청약", "전세", "임대", "재건축", "재개발",
             "주거", "전세사기", "공급대책", "모듈러",
             # 놓쳤던 유형 보강: 입주·허위매물·부동산정책·집값 등
             "입주", "허위매물", "부동산정책", "부동산 정책", "집값", "월세", "매매"]),
]
# 제목 키워드 오매칭 방지용 '가짜 친구'. 제목 매칭 전에 제거한다.
#   전세버스/전세기(=charter), 전세계(=whole world) 의 '전세'가 주택으로 잘못 잡히는 것 차단.
TITLE_FALSE_FRIENDS = ["전세버스", "전세기", "전세계", "월세계", "임대료 인상 없는"]


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    return s


def classify(dept: str, title: str) -> str | None:
    """담당부서(1순위) → 제목(2순위) 순으로 카테고리 판별. 해당 없으면 None."""
    for category, words in DEPT_RULES:
        if any(w in dept for w in words):
            return category
    # 제목 매칭 전 '가짜 친구' 제거 (전세버스→'전세' 오매칭 방지)
    scrubbed = title
    for ff in TITLE_FALSE_FRIENDS:
        scrubbed = scrubbed.replace(ff, "")
    for category, words in TITLE_RULES:
        if any(w in scrubbed for w in words):
            return category
    return None


def _fetch_list(session: requests.Session) -> list[dict]:
    """보도자료 목록 → [{title, url, field, pub_date}]  (분야로 거르지 않음)"""
    resp = session.get(
        LIST_URL, params={"psize": str(LIST_SIZE), "lcmspage": "1"}, timeout=20
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for tr in soup.select("tbody tr"):
        link = tr.select_one("td.bd_title a[href]")
        if not link:
            continue
        title = " ".join(link.get_text().split())
        if not title:
            continue
        field_td = tr.select_one("td.bd_field")
        date_td = tr.select_one("td.bd_date")
        items.append({
            "title": title,
            "url": urljoin(LIST_URL, link["href"]),
            "field": field_td.get_text(strip=True) if field_td else "",
            "pub_date": date_td.get_text(strip=True) if date_td else "",
        })
    return items


def _fetch_dept(session: requests.Session, url: str, retry: int = 3) -> str | None:
    """상세 페이지에서 '담당부서' 추출.

    반환값 구분이 중요하다.
      · 문자열  — 조회 성공 (빈 문자열이면 담당부서 표기가 없는 것)
      · None    — 조회 자체 실패. 이 경우 호출 측은 '본 것'으로 기록하지 말고
                  다음 주기에 다시 시도해야 한다. 예전에는 실패를 빈 문자열로
                  뭉개서 미분류로 폐기하고 재시도도 하지 않았다.
    """
    for attempt in range(1, retry + 1):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            break
        except requests.RequestException as e:
            logger.warning(f"[MOLIT] 상세 조회 실패 {url[:70]} 시도 {attempt}/{retry}: {e}")
            if attempt < retry:
                time.sleep(2 ** attempt)
            else:
                return None

    soup = BeautifulSoup(resp.text, "html.parser")
    info = soup.select_one("ul.bd_view_ul_info") or soup
    text = " ".join(info.get_text(" ", strip=True).split())
    m = re.search(r"담당부서\s*(.+?)\s*등록일", text)
    return m.group(1).strip() if m else ""


def fetch_new(topic_key: str, categories: list[str]) -> list[dict]:
    """신규 보도자료를 카테고리 우선순위 순으로 반환.

    categories: 수집할 카테고리를 우선순위 순으로 나열 (예: ["철도","도로","택지","주택"]).
                여기에 없는 카테고리(항공·건설 등)는 버린다.

    반환: [{"source":"molit", "category":..., "dept":..., "title":..., "url":...}, ...]
    """
    session = _new_session()
    try:
        listed = _fetch_list(session)
    except requests.RequestException as e:
        logger.error(f"[MOLIT] 목록 조회 실패: {e}")
        return []

    if not listed:
        logger.warning("[MOLIT] 목록 파싱 결과 0건 (사이트 구조 변경 의심)")
        return []

    # 상세 조회 전에 신규만 추림 (URL 완전일치 기준 — 제목 유사도는 쓰지 않는다.
    # 같은 사안의 네이버 기사와 제목이 비슷하다고 공식 발표가 누락되면 안 되므로)
    fresh = cache.filter_new_urls(listed, topic_key, SEEN_NS)
    if not fresh:
        logger.info(f"[MOLIT:{topic_key}] 신규 보도자료 없음 (목록 {len(listed)}건)")
        return []

    results = []
    resolved = []          # 상세 조회에 성공한 건만 '본 것'으로 기록한다
    for item in fresh:
        dept = _fetch_dept(session, item["url"])
        time.sleep(DETAIL_DELAY)
        if dept is None:
            # 조회 실패 → seen 기록하지 않고 다음 주기에 재시도
            logger.warning(f"[MOLIT:{topic_key}] 상세 조회 실패로 보류(다음 주기 재시도): {item['title'][:40]}")
            continue
        resolved.append(item)
        category = classify(dept, item["title"])

        if category not in categories:
            logger.info(
                f"[MOLIT:{topic_key}] 제외(대상 외: {category or '미분류'}, 부서={dept or '?'}): {item['title'][:40]}"
            )
            continue

        results.append({
            "source": "molit",
            "category": category,
            "dept": dept,
            "title": item["title"],
            "url": item["url"],
        })

    # 상세 조회에 성공한 건만 '본 것'으로 기록 → 조회 실패분은 다음 주기에 재시도된다
    cache.mark_urls_sent(resolved, topic_key, SEEN_NS)

    # 우선순위 정렬: categories 리스트 순서(철도 → 도로 → 택지 → 주택)
    order = {c: i for i, c in enumerate(categories)}
    results.sort(key=lambda n: order.get(n["category"], len(order)))

    logger.info(
        f"[MOLIT:{topic_key}] 신규 {len(fresh)}건(조회성공 {len(resolved)}) 중 대상 {len(results)}건: "
        + ", ".join(f"{n['category']}" for n in results)
    )
    return results


def fetch_from_items(topic_key: str, categories: list[str], items: list[dict]) -> list[dict]:
    """오늘장 피드가 준 보도자료(담당부서 조회까지 끝난 행)로 fetch_new 와 같은 결과를 만든다.

    국토부 사이트를 부르지 않는다 — 목록·상세 조회는 오늘장이 했다. 그 뒤는 fetch_new 와 같다:
    URL 완전일치 신규 판별(molit seen) → classify(담당부서, 제목) → seen 기록 → 우선순위 정렬.
    URL 은 정규화하지 않고 그대로 쓴다(오늘장도 urljoin(LIST_URL, href) 그대로 저장한다 —
    직접 수집과 같은 키가 나와야 원천이 바뀌어도 두 번 나가지 않는다).

    dept 가 None 인 행(상세 미조회·실패)은 받지 않는다 — 오늘장이 다시 조회한 뒤에 다시 온다.
    seen 에도 기록하지 않으므로 '실패분은 seen 에 기록하지 않음' 불변조건이 유지된다.
    """
    listed = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        url = it.get("url")
        title = it.get("title")
        dept = it.get("dept")
        if not isinstance(url, str) or not url or not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(dept, str):
            continue
        listed.append({
            "title": " ".join(title.split()),
            "url": url,
            "field": it.get("field") or "",
            "pub_date": it.get("pub_date") or "",
            "dept": dept.strip(),
        })

    if not listed:
        logger.info(f"[MOLIT:{topic_key}] 오늘장 피드: 새 보도자료 없음")
        return []

    fresh = cache.filter_new_urls(listed, topic_key, SEEN_NS)
    # 오늘장은 국토부 링크를 https://www.molit.go.kr/ 로만 준다(절대 http 주소는 https 로 바꿔 싣는다).
    # 직접 수집이 http 키로 seen 에 남긴 적이 있으면 같은 보도자료가 한 번 더 나간다 —
    # 비교할 때만 http 쌍둥이도 본다(저장 형식은 그대로). 2026-11-20 기준 seen 92건은 전부 https.
    fresh = [it for it in fresh
             if not (it["url"].startswith("https://")
                     and not cache.filter_new_urls([{"url": "http://" + it["url"][len("https://"):]}],
                                                   topic_key, SEEN_NS))]
    if not fresh:
        logger.info(f"[MOLIT:{topic_key}] 신규 보도자료 없음 (오늘장 피드 {len(listed)}건)")
        return []

    results = []
    for item in fresh:
        dept = item["dept"]
        category = classify(dept, item["title"])
        if category not in categories:
            logger.info(
                f"[MOLIT:{topic_key}] 제외(대상 외: {category or '미분류'}, 부서={dept or '?'}): {item['title'][:40]}"
            )
            continue
        results.append({
            "source": "molit",
            "category": category,
            "dept": dept,
            "title": item["title"],
            "url": item["url"],
        })

    # 피드 행은 전부 담당부서까지 조회된 것이라 모두 '본 것'으로 기록한다.
    cache.mark_urls_sent(fresh, topic_key, SEEN_NS)

    order = {c: i for i, c in enumerate(categories)}
    results.sort(key=lambda n: order.get(n["category"], len(order)))

    logger.info(
        f"[MOLIT:{topic_key}] 오늘장 피드 신규 {len(fresh)}건 중 대상 {len(results)}건: "
        + ", ".join(f"{n['category']}" for n in results)
    )
    return results
