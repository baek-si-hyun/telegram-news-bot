"""
뉴스 관련성 분류 모델 학습 스크립트 (토픽별)

토픽의 히스토리 JSONL에서 학습 데이터를 추출하고
TF-IDF + LogisticRegression 모델을 학습하여 저장합니다.

사용법:
    python ml/train_model.py <topic>        # 예: python ml/train_model.py realestate
    python ml/train_model.py                # 인자 없으면 모든 토픽 학습 시도

모델 파일:
    ml/models/<topic>.pkl   (학습된 모델)
    ml/reports/<topic>.txt  (성능 리포트)

데이터가 부족하면 (양성/음성 각 50건 미만) 학습하지 않습니다.
"""

import json
import os
import sys
import pickle
import re
from glob import glob

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.model_selection import cross_val_score
from sklearn.metrics import classification_report

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TOPICS  # noqa: E402

ML_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ML_DIR, "models")
REPORTS_DIR = os.path.join(ML_DIR, "reports")
STORAGE_DIR = os.path.join(os.path.dirname(ML_DIR), "storage")


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&quot;", '"').replace("&amp;", "&")
    return text.strip()


def load_data(topic: str, relevant_terms: list[str]):
    """토픽 히스토리에서 학습 데이터 추출 (not_press 제외, 자동 라벨링)"""
    texts, labels, seen_urls = [], [], set()
    history_dir = os.path.join(STORAGE_DIR, topic, "history")

    for filepath in sorted(glob(os.path.join(history_dir, "*.jsonl"))):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if d.get("reject_reason") == "not_press":
                    continue
                url = d.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title = _clean(d.get("title", ""))
                desc = _clean(d.get("description", ""))
                text = f"{title} {desc}"
                label = 1 if any(t in text for t in relevant_terms) else 0
                texts.append(text)
                labels.append(label)

    return texts, labels


def train(topic: str):
    profile = TOPICS.get(topic)
    if not profile:
        print(f"[{topic}] 알 수 없는 토픽 — 건너뜀")
        return
    relevant_terms = profile.get("ml_relevant_terms", [])

    texts, labels = load_data(topic, relevant_terms)
    pos = sum(labels)
    neg = len(labels) - pos
    print(f"[{topic}] 데이터: 총 {len(labels)}건 (관련 {pos} / 무관 {neg})")

    if pos < 50 or neg < 50:
        print(f"[{topic}] 데이터 부족 (양성/음성 각 50건 이상 필요). 학습 중단.")
        return

    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(max_features=5000, analyzer="char_wb", ngram_range=(2, 4), min_df=2)),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced", C=1.0)),
    ])

    scores = cross_val_score(pipeline, texts, labels, cv=5, scoring="f1")
    print(f"[{topic}] 5-Fold F1: {scores.mean():.3f} (+/- {scores.std():.3f})")

    pipeline.fit(texts, labels)
    preds = pipeline.predict(texts)
    report = classification_report(labels, preds, target_names=["무관", "관련"])
    print(report)

    os.makedirs(MODELS_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(os.path.join(MODELS_DIR, f"{topic}.pkl"), "wb") as f:
        pickle.dump(pipeline, f)
    with open(os.path.join(REPORTS_DIR, f"{topic}.txt"), "w", encoding="utf-8") as f:
        f.write(f"[{topic}] 데이터: {len(labels)}건 (관련 {pos} / 무관 {neg})\n")
        f.write(f"5-Fold F1: {scores.mean():.3f} (+/- {scores.std():.3f})\n\n")
        f.write(report)
    print(f"[{topic}] 모델 저장: ml/models/{topic}.pkl")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        train(sys.argv[1])
    else:
        for t in TOPICS:
            train(t)
