"""
학습된 모델로 뉴스 관련성을 판별하는 모듈 (토픽별 모델)

ml/models/<topic>.pkl 이 없으면 ML 분류를 건너뛰고 가중 점수만 사용합니다.
"""

import os
import pickle
import logging

logger = logging.getLogger(__name__)

ML_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ML_DIR, "models")

# 토픽별 모델 캐시: {topic: (model_or_None)}
_models: dict[str, object] = {}
_loaded: set[str] = set()


def _model_path(topic: str) -> str:
    return os.path.join(MODELS_DIR, f"{topic}.pkl")


def _load_model(topic: str):
    if topic in _loaded:
        return _models.get(topic)
    _loaded.add(topic)
    path = _model_path(topic)
    if not os.path.exists(path):
        logger.info(f"[ML:{topic}] 모델 없음 → ML 분류 비활성")
        _models[topic] = None
        return None
    try:
        with open(path, "rb") as f:
            _models[topic] = pickle.load(f)
        logger.info(f"[ML:{topic}] 모델 로드 완료")
    except Exception as e:
        logger.error(f"[ML:{topic}] 모델 로드 실패: {e}")
        _models[topic] = None
    return _models[topic]


def predict(title: str, description: str, topic: str) -> float | None:
    """관련성 확률 반환 (0.0~1.0). 토픽 모델 없으면 None"""
    model = _load_model(topic)
    if model is None:
        return None
    try:
        text = f"{title} {description}"
        proba = model.predict_proba([text])[0]
        return float(proba[1])
    except Exception as e:
        logger.error(f"[ML:{topic}] predict 실패: {e}")
        return None


def is_relevant(title: str, description: str, topic: str, threshold: float = 0.5) -> bool | None:
    """관련 기사인지 판별. 토픽 모델 없으면 None"""
    prob = predict(title, description, topic)
    if prob is None:
        return None
    return prob >= threshold
