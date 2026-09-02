import logging
import logging.handlers
import os
import signal
import sys
import schedule
import time
from config import validate_config, PROMO_TIME, PROMO_WEEKDAY, topic_status
from scheduler import run_cycle, send_help, report_downtime
from bot import commands

# logs 디렉터리 보장 (스크립트 위치 기준)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# 로깅 설정 (5MB * 3 파일 rotation)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            os.path.join(LOGS_DIR, "bot.log"),
            encoding="utf-8",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        ),
    ],
)
logger = logging.getLogger(__name__)

# Graceful shutdown
_running = True


def _shutdown(signum, frame):
    global _running
    logger.info("종료 신호 수신, 안전하게 종료합니다...")
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def main():
    logger.info("뉴스 텔레그램 봇 시작 (멀티 토픽)")

    # 환경변수 유효성 검사
    try:
        validate_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    # 토픽 상태 로깅
    for line in topic_status():
        logger.info(f"토픽 - {line}")

    # 키워드 관리 명령(/add·/del·/list) 수신 폴링 시작 (관리자 미설정 시 자동 비활성)
    commands.start_polling(lambda: _running)

    # 매시 정각·30분에 수집 → 즉시 전송 (국토부 보도자료 먼저, 그다음 네이버 뉴스)
    # every(30).minutes 는 '직전 실행 종료 + 30분'이라 한 주기(약 1.4분)만큼 계속 밀렸다.
    # 실측 평균 31.38분 → 하루 46회로, 48회여야 할 것에서 2회를 잃었다.
    # 고정 시각(:00, :30)에 걸면 드리프트가 누적되지 않는다.
    schedule.every().hour.at(":00").do(run_cycle)
    schedule.every().hour.at(":30").do(run_cycle)

    # 키워드 명령 사용법 안내 — 매일 보내면 채널이 지저분해져 주 1회로 줄였다.
    getattr(schedule.every(), PROMO_WEEKDAY).at(PROMO_TIME).do(send_help)

    logger.info(f"스케줄: 매시 :00/:30 수집→즉시 전송 / 명령안내 {PROMO_WEEKDAY} {PROMO_TIME}")

    # 직전 주기 이후 공백이 컸으면(호스트 종료 등) 복구 시점에 알린다.
    try:
        report_downtime()
    except Exception as e:  # noqa: BLE001
        logger.error(f"가동중단 확인 실패: {e}")

    # 기동 직후 1회 즉시 수집.
    # schedule 은 첫 실행을 30분 뒤로 잡기 때문에, 재기동할 때마다 30분 공백이 생겼다.
    # 네이버는 키워드당 최신 30건만 주므로 그 공백은 그대로 기사 유실이 된다.
    logger.info("기동 직후 1회 수집 실행")
    try:
        run_cycle()
    except Exception as e:  # noqa: BLE001
        logger.error(f"기동 직후 수집 실패: {e}")

    while _running:
        schedule.run_pending()
        time.sleep(10)

    logger.info("봇 종료 완료")


if __name__ == "__main__":
    main()
