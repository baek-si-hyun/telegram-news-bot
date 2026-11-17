# CLAUDE.md — telegram-news-bot

네이버 뉴스 Open API + 국토교통부 보도자료를 30분마다 수집 → 판정 → 중복 제거 → 토픽별 텔레그램 채널로
즉시 전송하는 Python 봇. 프론트 없음. 한 프로세스가 여러 토픽(`sto`, `realestate`)을 돈다.
아래는 전부 코드에서 확인한 사실이고 근거를 `파일:줄`로 단다. README 는 2026-11-17 에 코드 기준으로 정정했다 — 둘이 어긋나면 코드가 맞다.

## 실행 · 검사

- 운영: `docker compose up -d --build` (README.md:20-25). 컨테이너 `telegram-news-bot`, `restart: unless-stopped`,
  볼륨은 `./storage`·`./logs` 만 (docker-compose.yml:6-12). TZ 는 `Asia/Seoul` (Dockerfile:5, docker-compose.yml:14).
- healthcheck 는 `logs/bot.log` mtime 이 1시간 넘게 안 바뀌면 unhealthy (docker-compose.yml:15-16).
- ⚠️ **`python main.py` 를 로컬에서 돌리면 실제 채널로 발송된다** — 기동 직후 `run_cycle()` 을 바로 부른다(main.py:86-88).
  또 명령 폴링(getUpdates)이 봇 토큰별로 뜨므로(bot/commands.py:388-409) 운영 컨테이너와 같은 토큰으로 겹쳐 돈다.
  시험 발송 금지. 동작 확인은 함수 단위로 하고 `telegram.send_news` 를 부르지 않는다.
- 테스트 스위트는 없다. 최소 검사: `python -m py_compile main.py scheduler.py config.py unified.py crawler/*.py bot/*.py storage/*.py ml/*.py`
- 의존성은 고정 버전 (requirements.txt:1-8). `scikit-learn==1.8.0` 은 **고정 유지** — 모델 pickle 이 버전에 묶인다(requirements.txt:5-7).

## 구조

| 파일 | 역할 |
|---|---|
| main.py | 로깅(5MB×3, main.py:18-31) · 스케줄 등록 · 기동 직후 1회 수집 · graceful shutdown |
| scheduler.py | `run_cycle()` 토픽 루프: 국토부 → 수집 → 전송 (scheduler.py:181-208) |
| config.py | 공통 상수 + 토픽 프로필 `TOPICS` (config.py:63-329), 활성 판정 `active_topics()` (config.py:337) |
| crawler/naver.py | 네이버 검색 + 판정 게이트 + 히스토리 기록 (`search`, crawler/naver.py:141) |
| crawler/molit.py | 국토부 보도자료 목록/상세 파싱, 담당부서→제목 순 분류 (crawler/molit.py:51-91) |
| crawler/body.py | 기사 본문 수집(본문 2단계 판정용, 8스레드·8초 타임아웃, crawler/body.py:31-33) |
| bot/telegram.py | sendMessage / 보도자료는 sendPhoto+캡션, 채팅방당 1초 간격(bot/telegram.py:17), 토큰 마스킹(bot/telegram.py:37) |
| bot/commands.py | `/add` `/del` `/list` 폴링 + 권한 판정 |
| storage/cache.py | 중복 제거(URL 정규화 + 제목 유사도), 네임스페이스별 '본 URL' |
| storage/pending.py | 수집→전송 사이 대기 큐 |
| storage/history.py | 월별 감사로그 jsonl |
| storage/keywords.py | 런타임 키워드(`/add`·`/del`)를 config 위에 병합 (storage/keywords.py:138) |
| storage/heartbeat.py | 주기 완료 시각 기록 → 기동 시 정지 구간 알림 |
| ml/classifier.py · ml/relevance.py · ml/train_model.py | 추론 · 모델 클래스 · 학습 |
| unified.py | (선택) 보낸 뉴스를 통합 포스팅 앱에도 기록 |

## 흐름

1. 스케줄은 **고정 시각** `:00`/`:30` (main.py:69-70). 명령 안내는 주 1회 월요일 13:05 (main.py:73, config.py:33-34).
2. 기동 시 `report_downtime()` — 직전 주기 후 90분 넘게 비었으면 각 채널에 알림 (main.py:79, scheduler.py:211-225).
3. 토픽마다 (scheduler.py:187-205):
   - `_molit_topic` 국토부 보도자료 먼저 → 점수 필터 없이 즉시 전송 (scheduler.py:144-166). `molit_categories` 가 있는 토픽만(현재 realestate, config.py:327).
   - `_collect_topic` 키워드마다 `naver.search` → `cache.filter_new` → `_refine_with_body` → `pending.add` (scheduler.py:70-112).
   - `_send_topic` 큐를 peek 해서 보내고, **성공분만** 캐시 기록 + 큐 제거 (scheduler.py:115-141).
   - 각 단계 예외는 해당 채널로 `_alert` (scheduler.py:169-178).
4. 주기 끝에 `heartbeat.touch()` (scheduler.py:208).

### 네이버 판정 순서 — crawler/naver.py:185-309

| 순서 | 게이트 | reject_reason | 근거 |
|---|---|---|---|
| 1 | 신선도: 발행 후 `max_article_age_days`(30일) 초과 컷, pub_date 파싱 실패면 통과 | `too_old` | naver.py:209-215, config.py:20 |
| 2 | 제휴사: 링크가 `n.news.naver.com`/`news.naver.com` 아니면 컷. 제목에 `partner_bypass_terms` 있으면 우회 | `not_press` | naver.py:221-227 |
| 3 | 핵심어 `require_any_terms` (sto 만) | `no_core_term` | naver.py:232-237, config.py:92 |
| 4 | `strict_match` (현재 둘 다 빈 set) | `keyword_too_few` | naver.py:240-247, config.py:85 |
| 5a | **모델 있으면 모델이 판정** — 확률 < 컷오프면 컷. 우선순위 고유명사가 제목에 있으면 `ml_priority_cutoff` | `ml_reject` | naver.py:261-280 |
| 5b | 모델 없을 때만 폴백: 점수 < threshold, 제목 게이트 | `low_score` / `title_off_topic` | naver.py:281-295 |

- 점수는 항상 계산·기록한다(naver.py:250-252). 제목 단어는 2배, 긴 항목부터 매칭하고 구간을 소거해 상쇄·중복가산을 막는다(naver.py:18-57).
- realestate 는 본문 2단계: 수집 단계는 잠정선 `ml_provisional_cutoff`(0.35)만 적용(naver.py:173-175, config.py:217) →
  중복 제거 **뒤** 남은 것만 본문 받아 최종 판정(scheduler.py:16-67). 본문 실패 시 스니펫 확률로 폴백(scheduler.py:49-51).
  끄는 스위치 `use_body_model` (config.py:214).
- `not_press` 는 히스토리에 안 남기고, 나머지도 (토픽, URL) 당 1회만 기록(naver.py:66-81).

### 중복 제거 — storage/cache.py

- 네이버: 정규화 URL(추적 파라미터 제거, cache.py:42-60) + 제목 유사도(cache.py:79-101). 보관 `CACHE_EXPIRE_DAYS=45`일 (config.py:26).
- 네임스페이스 '본 URL'(`molit`, `body_eval`): URL 일치만, 30일 보관 (cache.py:189, 228-258).

## 되돌리면 재발하는 불변조건 (메모리 '운영 불변조건' + 코드 주석)

- **`pending.pop_all()` 쓰지 말 것** — 전송 전에 큐를 비워 도중에 죽으면 배치 유실(storage/pending.py:76-85). `peek_all` + `remove_sent` 사용.
- `cache.mark_all_sent()` 는 전송 **성공 후**에만 (scheduler.py:106-108, 136-140).
- 국토부 상세 조회 실패 건은 `seen` 에 기록하지 않는다(재시도 기회 보존).
- `schedule.every(30).minutes` 금지 — 드리프트 누적(main.py:66-68). 기동 직후 `run_cycle()` 1회 필수(main.py:83-90).
- **모델은 점수 임계값을 '대체'한다. 규칙 위에 얹는 하이브리드 금지** (naver.py:255-260). 제휴사·핵심어 게이트는 위생이라 유지.
- 키워드·검색어는 **사용자 자산** — 임의로 바꾸지 않는다. 정확일치 따옴표는 사용자 승인분만: `리턴플러스`(config.py:78), `평창터널`(config.py:162).
- 수작업 특징을 모델에 다시 넣지 않는다(역효과 실측). 가중치 점수도 특징으로 안 쓴다(ml/train_model.py:75-77, `use_rule_score: False`).

## ML

- 모델 파일: `ml/models/<topic>.pkl`, `ml/models/realestate_body.pkl` (classifier.py:30-35). 없거나 로드 실패면 규칙 폴백(classifier.py:44-59).
- 모델 클래스는 `ml/relevance.py` 에 둔다 — pickle 이 import 경로를 저장한다(classifier.py:20-23).
- 학습: `python ml/train_model.py [topic] [--min-precision=0.85]` (ml/train_model.py:282-291). 본문 모델은 `BODY_CFG` 토픽(realestate)만 자동 추가 학습(ml/train_model.py:98-101).
- 학습 데이터는 `ml/data/<topic>.jsonl` 의 **규칙과 독립된 라벨**(`y`, `group`, `confidence`) (ml/train_model.py:20-28, 134-161). 클래스당 최소 150건(ml/train_model.py:62).
  확신도 가중 `CONFIDENCE_WEIGHT` (ml/train_model.py:70). 평가는 GroupKFold out-of-fold 만.
- ⚠️ 예전 라벨 = 키워드 규칙(순환)이었다 — 그 시절 리포트 F1 0.97 은 무의미(ml/train_model.py:3-16). 라벨 예산은 '더 모으기'가 아니라 '있는 라벨 고치기'에 쓴다(메모리 'ML 라벨 순환').
- F1 은 **같은 고정 테스트셋**에서만 비교한다.
- `ml/models/` 는 `.dockerignore` 에 없어 `COPY . .` 로 이미지에 구워진다(Dockerfile:18, .dockerignore 전체). 재학습 반영 = 재빌드.

## 데이터 · 비밀

- 커밋 금지(.gitignore): `.env`, `storage/*/history/`(수 GB), `storage/*/{sent_news,pending_news,keywords,molit_seen,body_eval_seen}.json`,
  `storage/updates_offset.json`(토큰이 키), `storage/heartbeat.json`, `ml/models/*.pkl`, `ml/reports/*.txt`, `ml/data/*_bodies.jsonl`(언론사 원문), `logs/`.
- `ml/data/<topic>.jsonl` 라벨은 커밋한다(.gitignore:16-18).
- 환경변수 목록은 `.env.example`. 토픽은 봇 토큰 + 채팅방 ID 둘 다 있어야 활성(config.py:344). 네이버 키 없으면 기동 실패(config.py:365-378).
- `UNIFIED_API_URL`·`UNIFIED_INGEST_TOKEN` 둘 다 있을 때만 통합 앱 기록, 실패해도 예외 없음(unified.py:48-70). 토픽 매핑 realestate→`property` (unified.py:39-42).

## 외부 접근 주의

- 네이버는 공식 Open API(`openapi.naver.com/v1/search/news`, crawler/naver.py:15). 키워드당 30건·1.5초 간격(config.py:28-30), 429 는 지수 백오프(naver.py:125-128).
- 외부 요청 UA 는 봇임을 밝힌다(`telegram-news-bot/1.0 (+repo URL)`): 국토부 crawler/molit.py:39, 본문 수집 crawler/body.py:37-40.
  **봇 차단(403·챌린지)을 UA·헤더 위장으로 우회하지 않는다.** 본문이 막히면 스니펫 판정으로 폴백한다(scheduler.py:49-51).
- 국토부는 세션 쿠키 유지가 필수(첫 307 에서 쿠키를 심는다, crawler/molit.py:6-9). 담당부서를 주는 공식 대체 경로는 없다
  — 정책브리핑 RSS 종료, data.go.kr 에 국토부 보도자료 API 없음, molit RSS 는 담당부서 없음·10건 (crawler/molit.py:10-11).
- 국토부 동작 확인 때 `molit.fetch_new()` 를 부르지 않는다 — 운영 `storage/realestate/molit_seen.json` 에 '본 것'을 찍어 실제 전송이 빠진다
  (crawler/molit.py:205). `_fetch_list`·`_fetch_dept`·`classify` 만 쓴다.

## 운영 수칙

- 이 PC 는 운영 서버다. 평일 14:45~15:15 · 19:30~19:50 에는 빌드·`docker compose` 재기동·무거운 작업 금지.
- 돌고 있는 컨테이너를 멈추거나 재기동하지 않는다(필요하면 팀장에게).
- 미해결(사용자만 가능): 봇 토큰 재발급 + 토픽별 토큰 분리(`.env.example:1-7`).
