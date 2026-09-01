"""학습된 모델로 뉴스의 게시 적합성을 판별한다 (토픽별 모델).

`ml/models/<topic>.pkl` 이 없으면 None 을 돌려주고, 호출 측(crawler/naver.py)은
기존 가중치 규칙으로 판정한다. 즉 모델은 **있으면 쓰고 없으면 규칙**이다.

모델 객체의 클래스는 ml/relevance.py 에 있다 — pickle 은 클래스의 import 경로를
같이 저장하므로 학습·추론이 같은 모듈을 봐야 한다.
"""

import logging
import os
import pickle
import sys

logger = logging.getLogger(__name__)

ML_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ML_DIR, "models")

# 언피클에 ml.relevance 가 필요하다. 패키지 루트가 sys.path 에 없을 수도 있어 보강한다.
_ROOT = os.path.dirname(ML_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 토픽별 모델 캐시: {topic: model_or_None}
_models: dict[str, object] = {}
_loaded: set[str] = set()


def _model_path(key: str) -> str:
    return os.path.join(MODELS_DIR, f"{key}.pkl")


def _body_key(topic: str) -> str:
    return f"{topic}_body"


def _load_model(topic: str):
    """topic 또는 '<topic>_body' 를 키로 받는다."""
    if topic in _loaded:
        return _models.get(topic)
    _loaded.add(topic)
    path = _model_path(topic)
    if not os.path.exists(path):
        logger.info(f"[ML:{topic}] 모델 없음 → 가중치 규칙으로 판정")
        _models[topic] = None
        return None
    try:
        with open(path, "rb") as f:
            model = pickle.load(f)
        _models[topic] = model
        m = getattr(model, "metrics", {}) or {}
        logger.info(
            f"[ML:{topic}] 모델 로드 완료 (컷오프 {getattr(model, 'cutoff', 0.5):.2f}"
            f", 학습 {m.get('n', '?')}건, F1 {m.get('f1', float('nan')):.3f})"
        )
    except Exception as e:
        logger.error(f"[ML:{topic}] 모델 로드 실패 → 규칙으로 판정: {e}")
        _models[topic] = None
    return _models[topic]


def predict(title: str, description: str, topic: str, rule_score=None) -> float | None:
    """게시 적합 확률 (0.0~1.0). 토픽 모델이 없으면 None."""
    model = _load_model(topic)
    if model is None:
        return None
    try:
        return model.probability(title, description, rule_score)
    except Exception as e:
        logger.error(f"[ML:{topic}] predict 실패 → 규칙으로 판정: {e}")
        return None


def has_body_model(topic: str) -> bool:
    """본문 모델이 있는 토픽인지. 있으면 수집 단계에서 잠정 통과선을 낮춘다."""
    return _load_model(_body_key(topic)) is not None


def predict_body(title: str, description: str, topic: str, body: str,
                 rule_score=None) -> float | None:
    """본문까지 넣은 확률. 본문 모델이 없거나 본문이 부실하면 None.

    None 이면 호출 측이 스니펫 판정을 그대로 쓴다 — 본문 수집 실패가 흐름을 막지 않는다.
    """
    if not body or len(body) < 200:
        return None
    model = _load_model(_body_key(topic))
    if model is None:
        return None
    try:
        return model.probability(title, description, rule_score, body)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[ML:{topic}] 본문 predict 실패 → 스니펫 판정 사용: {e}")
        return None


def body_cutoff(topic: str) -> float | None:
    model = _load_model(_body_key(topic))
    return None if model is None else float(getattr(model, "cutoff", 0.5))


def cutoff(topic: str) -> float | None:
    """해당 토픽 모델의 운영 컷오프. 모델이 없으면 None."""
    model = _load_model(topic)
    if model is None:
        return None
    return float(getattr(model, "cutoff", 0.5))


def is_relevant(title: str, description: str, topic: str,
                threshold: float | None = None, rule_score=None):
    """(적합여부, 확률) 반환. 모델이 없으면 (None, None).

    threshold 를 주지 않으면 모델에 저장된 운영 컷오프를 쓴다.
    """
    prob = predict(title, description, topic, rule_score)
    if prob is None:
        return None, None
    thr = threshold if threshold is not None else (cutoff(topic) or 0.5)
    return prob >= thr, prob
