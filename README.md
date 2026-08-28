# telegram-news-bot

네이버 뉴스 Open API 와 국토교통부 보도자료를 **30분마다 수집 → 관련성 필터 → 중복 제거 →
토픽마다 다른 텔레그램 방으로 즉시 전송**하는 봇. 한 프로세스가 여러 토픽을 함께 돌린다.

현재 토픽 두 개:

| 토픽 키 | 내용 | 소스 |
|---|---|---|
| `sto` | STO·토큰증권·조각투자 | 네이버 뉴스 |
| `realestate` | 부동산·철도·택지 | 네이버 뉴스 + **국토교통부 보도자료** |

두 토픽은 **서로 다른 텔레그램 채널**로 나간다. 토픽 사이의 차이는 `config.py` 의 프로필 한 블록뿐이고,
수집·필터·중복제거·ML·전송·스케줄은 전부 공통 코드다.

---

## 빠른 시작

```bash
cp .env.example .env          # 값 채우기 (아래 '환경변수' 참고)
docker compose up -d --build  # 빌드 + 기동
docker compose logs -f        # 로그
docker compose down           # 중지
```

도커 없이 직접:

```bash
python -m venv .venv && .venv/Scripts/activate   # linux/mac: source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

기동하면 `validate_config()` 가 네이버 키와 **구동 가능한 토픽이 하나라도 있는지** 검사하고,
없으면 사유를 찍고 종료한다. 토픽은 **봇 토큰 + 채팅방 ID 가 둘 다 있어야** 활성화된다.

---

## 동작 흐름

```
30분마다 run_cycle()  ─ 토픽마다:
   ① 국토부 보도자료   molit.fetch_new()   → URL 중복제거 → 즉시 전송   (부동산 토픽만)
   ② 네이버 뉴스       naver.search()      → 필터 5단 → 대기 큐 → 전송
매일 13:05            send_help()          → 키워드 명령 사용법을 각 채널로 안내
```

- 보도자료를 **먼저** 보낸다 — 공식 발표가 기사 뒤에 묻히지 않게.
- 과거의 08:00·13:00·19:00 배치 전송은 폐지됐다. 속보성을 위해 수집한 주기에 바로 보낸다.
- 홍보 메시지(`send_promos`)는 스케줄에서 빠져 있다. 되돌리려면 [main.py](main.py) 의
  `send_help` 등록을 `send_promos` 로 바꾼다.

### 네이버 뉴스가 통과해야 하는 5단 필터 — [crawler/naver.py](crawler/naver.py)

| 순서 | 게이트 | 떨어지는 이유(`reject_reason`) |
|---|---|---|
| 1 | 제휴 언론사만 | 네이버 링크가 `n.news.naver.com` 이 아니면 컷 (`not_press`) — 블로그·카페 제외 |
| 2 | 핵심어 게이트 (선택) | `require_any_terms` 중 하나도 없으면 컷 (`no_core_term`) |
| 3 | strict_match (선택) | 제목+본문에 그 키워드가 2회 미만이면 컷 (`keyword_too_few`) |
| 4 | 가중 점수 | `score < threshold` 면 컷 (`low_score`). **제목에 있는 단어는 2배** |
| 5 | 제목 게이트 (선택) | `require_title_keyword` 인데 제목에 양성 키워드가 없으면 컷 (`title_off_topic`) |
| 6 | ML 보조 (모델 있을 때만) | 확률 < 0.3 이면 컷 (`ml_reject`) |

통과·탈락 **전부**가 `storage/<topic>/history/<YYYY-MM>.jsonl` 에 점수·사유와 함께 남는다.
필터를 손볼 때는 이 기록을 보고 판단하고, ML 학습 데이터로도 쓴다.

핵심어 게이트가 왜 필요했나 — STO 토픽에서 `증권`·`투자`·`토큰`·`블록체인` 같은 범용 금융어만으로
임계값을 넘겨 일반 코인·증권사 기사가 샜다. 제목 게이트는 반대로 부동산 토픽에서 본문에만
부동산 단어가 박힌 정치 기사를 막는다.

### 중복 제거 — [storage/cache.py](storage/cache.py)

- 네이버 뉴스: **URL 완전일치 + 제목 유사도**. 7일(`CACHE_EXPIRE_DAYS`) 보관.
- 국토부 보도자료: **URL 완전일치만**, 30일 보관(`filter_new_urls`, `molit` 네임스페이스).
  같은 사안의 기사와 제목이 비슷하다고 공식 발표가 누락되면 안 되기 때문이다.

---

## 구조

```
main.py              스케줄 등록 · 로깅(5MB×3 rotation) · graceful shutdown
scheduler.py         run_cycle(): 토픽 루프 — 보도자료 → 수집 → 전송
config.py            토픽 프로필 TOPICS = 키워드·가중치·임계값·채널·보도자료 카테고리
crawler/naver.py     네이버 검색 + 5단 필터 + 히스토리 기록
crawler/molit.py     국토부 보도자료 목록/상세 파싱 + 담당부서 기반 분류
bot/telegram.py      전송 (sendMessage / 보도자료는 sendPhoto + 캡션)
bot/commands.py      /add · /del · /list 폴링 처리 + 권한 판정
storage/cache.py     중복 제거 (URL·제목유사도, 토픽별·네임스페이스별)
storage/pending.py   수집→전송 사이의 대기 큐
storage/history.py   전체 수집 기록 (월별 jsonl, 필터 사유 포함)
storage/keywords.py  사용자가 명령으로 넣고 뺀 키워드 (config 위에 병합)
ml/classifier.py     토픽별 모델 로드·추론 (없으면 조용히 건너뜀)
ml/train_model.py    히스토리로 토픽별 모델 학습
```

데이터는 토픽별로 격리된다 — `storage/sto/`, `storage/realestate/`.
운영 데이터(히스토리·캐시·대기큐·사용자 키워드·모델)는 전부 `.gitignore` 에 있다.
**히스토리 jsonl 은 수 GB 까지 자란다.** 커밋하지 마라.

---

## 환경변수 — `.env`

`.env.example` 을 복사해서 채운다. `.env` 는 커밋 금지(`.gitignore`).

| 변수 | 설명 |
|---|---|
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | **필수.** 네이버 Open API (모든 토픽 공유) |
| `TELEGRAM_BOT_TOKEN` | 공용 봇 토큰. 토픽별 토큰이 없으면 이것을 쓴다 |
| `TELEGRAM_CHAT_IDS_STO` (또는 `TELEGRAM_CHAT_IDS`) | STO 채널 ID. 쉼표로 여러 개 |
| `TELEGRAM_CHAT_IDS_REALESTATE` | 부동산 채널 ID — **채우면 부동산 토픽이 켜진다** |
| `TELEGRAM_BOT_TOKEN_STO` / `..._REALESTATE` | (선택) 토픽 전용 봇을 따로 쓸 때만 |
| `TELEGRAM_ADMIN_IDS` | (선택) 슈퍼관리자 사용자 ID. 비워두면 '그 방의 관리자'가 기본 권한 |
| `ENABLED_TOPICS` | (선택) 구동 토픽 제한. 예: `sto,realestate` |

### 토픽 하나 켜는 법
1. 텔레그램에서 채널을 만들고 봇을 **관리자로** 추가한다.
2. `.env` 의 해당 `TELEGRAM_CHAT_IDS_*` 에 채널 ID 를 넣는다.
3. `docker compose up -d --build` — env 반영을 위해 recreate 가 필요하다.

---

## 키워드 명령 — `/add` · `/del` · `/list`

텔레그램 메시지로 검색 키워드를 실시간으로 넣고 뺀다. **코드 수정·재배포가 필요 없고,
다음 수집 주기(최대 30분)부터 반영**된다. 사용법 안내는 매일 13:05 각 채널로 자동 발송된다.

| 명령 | 설명 |
|---|---|
| `/list` | 현재 키워드 목록 (추가분 · 기본 · 비활성) |
| `/add <키워드>` | 추가. **입력한 문구 전체가 키워드이자 검색어** — 띄어쓰기·숫자 그대로. 가중치는 자동 |
| `/del <키워드>` | 추가 키워드는 삭제, 기본 키워드는 비활성 처리 (되돌리려면 다시 `/add`) |

```
/add 판교역
/add 청년 임대주택         # 띄어쓰기 OK → 키워드 = 검색어 = '청년 임대주택'
/add 구리트램 | 워커힐 재건축 | 양정역세권    # | 로 여러 개 한 번에
/del 청년 임대주택
/list
```

- **어느 토픽인지는 명령을 보낸 방으로 자동 판별**한다. 그 토픽 방에서 그냥 치면 된다.
  자동 판별이 안 되는 곳(봇과 1:1 등)에서는 앞에 토픽명을 붙인다 — `/add sto STO`.
- **권한**: 그 방의 관리자면 누구나. 채널은 관리자만 글을 올릴 수 있으므로 통과시키고,
  그룹은 `getChatMember` 로 실시간 확인한다. `TELEGRAM_ADMIN_IDS` 에 넣은 사용자는 DM 등
  어디서든 쓸 수 있다.
- 가중치는 그 토픽 기존 검색어들의 **중앙값**(대략 6)으로 자동 부여된다. 키워드는 검색어이자
  점수 가중치 키(부분일치)라, 띄어쓴 구는 구성 단어의 기본 가중치도 함께 더해져 필터를 통과한다.
- 저장 위치: `storage/<topic>/keywords.json` (config 를 덮지 않고 그 위에 병합).

---

## 국토교통부 보도자료 (부동산 토픽)

네이버 뉴스와 **별개 소스**다. 국토부 로고 카드 사진 + 캡션(`📢 [국토부·철도] 제목`) 형태로 보낸다.

| 항목 | 내용 |
|---|---|
| 소스 | `https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp` (서버렌더링 HTML, JS 불필요) |
| 접근 | ⚠️ **세션 쿠키 + 브라우저 UA 필수.** 없으면 같은 URL 로 307 무한 반복 → `requests.Session` 사용 |
| 분류 | 상세페이지의 **담당부서**(1순위) → **제목 키워드**(2순위) |
| 대상 | `config.py` 의 `molit_categories` — **나열 순서가 곧 전송 우선순위** (철도→도로→택지→주택) |
| 중복제거 | URL 완전일치만, 30일 보관 |

**설계 근거 (건드리기 전에 읽어라)**

- 국토부 자체 '분야'로는 4분류가 **불가능**하다 — `도로철도`(철도+도로), `주택토지`(택지+주택)로
  합쳐져 있다. 게다가 '인천 2호선 연장'이 `교통물류`, '전세사기피해자'가 `일반`으로 잡히는 등
  분야 값 자체가 부정확하다. 그래서 담당부서 + 제목으로 판별한다.
- **관련성 점수 필터를 태우지 않는다.** 국토부 공식 발표이고 담당부서로 이미 선별됐다.
  부동산 토픽의 점수 필터(`threshold=12` + `require_title_keyword`)를 태우면
  `[장관동정] 도로 침수 대비` 같은 발표가 잘려나간다.
- **사진으로 보내는 이유**: 국토부 상세페이지에 `og:image` 가 없어 링크 프리뷰 썸네일이 안 뜬다.
  썸네일이 붙는 네이버 뉴스 사이에서 텍스트만 있는 보도자료는 묻힌다. 카드는
  `bot/assets/molit_card.png` (1000×524). 첫 전송 후 `file_id` 를 재사용하고,
  카드 파일이 없으면 일반 텍스트 전송으로 자동 폴백한다.

카테고리를 바꾸려면 `TOPICS["realestate"]["molit_categories"]` 를 수정한다
(빈 리스트면 보도자료 수집이 꺼진다). 분류 규칙은 [crawler/molit.py](crawler/molit.py) 의
`DEPT_RULES` / `TITLE_RULES`.

---

## ML 보조 분류기 (선택)

히스토리가 쌓이면 토픽별 보조 분류기를 학습할 수 있다. **모델이 없으면 조용히 건너뛰고**
가중 점수만 쓴다 — 없어도 정상 동작한다.

```bash
python ml/train_model.py realestate    # 특정 토픽
python ml/train_model.py               # 전체 토픽
```

- 학습 데이터는 히스토리에서 `ml_relevant_terms` 로 자동 라벨링한다.
- 양성/음성 각 **50건 이상** 있어야 학습한다. 5-Fold F1 을 찍고 `ml/models/<topic>.pkl` 로 저장.
- 추론에서 확률 < 0.3 이면 컷(4단 점수 필터를 이미 통과한 기사에 대한 보수적 보조 판정).

---

## 토픽 추가하기

`config.py` 의 `TOPICS` 에 프로필 한 블록을 넣고 `.env` 에 채널을 채우면 끝. **코드 수정 불필요.**

```python
"새토픽": {
    "label": "표시 이름",
    "bot_token": _token("TELEGRAM_BOT_TOKEN_새토픽"),
    "chat_ids": _chat_ids("TELEGRAM_CHAT_IDS_새토픽"),
    "keywords": {"키워드": "실제 검색어", ...},   # 키 = 가중치 키, 값 = 네이버 쿼리
    "strict_match": set(),                       # 2회 이상 등장해야 통과할 키워드
    "threshold": 6,                              # 점수 임계값
    "positive_weights": {...}, "negative_weights": {...},
    "ml_relevant_terms": [...],                  # ML 자동 라벨링 기준어
    "promo": [...],                              # (보류 중인) 홍보 문구
}
```

선택 키 — `require_any_terms`(핵심어 게이트) · `require_title_keyword`(제목 게이트) ·
`molit_categories`(국토부 보도자료 수집).

---

## 운영 메모

- **레이트 리밋**: 키워드마다 1.5초 간격(`REQUEST_DELAY_SECONDS`), 전송은 채팅방당 1초(분당 20건 제한).
  네이버 429 · 텔레그램 429 는 지수 백오프로 재시도한다.
- **로그**: `logs/bot.log` (5MB × 3 rotation). 컨테이너 healthcheck 가 이 파일의 mtime 을 본다 —
  1시간 넘게 안 쓰이면 unhealthy.
- **타임존**: 컨테이너는 `Asia/Seoul` 고정. 스케줄 시각이 이 기준이다.
- **볼륨**: `./storage` 와 `./logs` 만 마운트한다. 컨테이너를 갈아엎어도 중복제거 상태와 기록은 남는다.
- **커밋 금지**: `.env`(봇 토큰) · `storage/updates_offset.json`(토큰이 키로 들어 있다) ·
  `storage/*/history/`(수 GB) · `ml/models/*.pkl`. 전부 `.gitignore` 에 있다.
