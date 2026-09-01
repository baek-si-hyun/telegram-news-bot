"""뉴스 게시 적합성 분류 모델 학습 (토픽별).

## 왜 다시 썼는가 — 이전 버전의 치명적 결함

이전 학습 스크립트의 라벨 생성식은 이랬다:

    label = 1 if any(t in text for t in relevant_terms) else 0

**라벨이 곧 키워드 규칙이었다.** 그래서 모델은 "뉴스가 관련 있는가"가 아니라 "이 문자열에
이 단어가 들어 있는가"를 학습했다. 실측하면 학습이 전혀 없는 0줄짜리 키워드 매칭의 정확도가
1.0000 이고(라벨 생성식과 동일식이니 당연), 그 라벨로 학습한 모델의 F1 은 0.975 였다 —
공짜 규칙을 근사하다 오히려 못 미친 것이다. 리포트의 F1 0.97 은 이래서 무의미했다.
게다가 리포트의 classification_report 는 fit() 한 데이터를 그대로 predict() 한 in-sample
수치였다(정직한 건 5-Fold 줄뿐).

**결론: 모델·하이퍼파라미터 튜닝은 소용없었다. 병목은 라벨이었다.**

## 지금 방식

학습 데이터는 `ml/data/<topic>.jsonl` 이다. 규칙과 **독립적으로** 만든 라벨이어야 한다
(사람이 직접 라벨링하거나, LLM 을 편집자로 세워 라벨링). 각 줄의 형식:

    {"title": ..., "description": ..., "y": 0|1, "group": "...", "rule_score": 12}

  · y        1 = 채널에 올릴 가치 있음, 0 = 부적합
  · group    near-duplicate 클러스터 id. 같은 사건 기사가 train/test 로 갈리면 점수가
             부풀려지므로 GroupKFold 로 묶어서 나눈다. 없으면 url/title 로 대체된다.
  · rule_score  crawler/naver.py 의 가중치 점수. 특징으로 함께 쓴다(선택).

평가는 **전부 out-of-fold** 다. in-sample 수치는 출력하지 않는다.

사용법:
    python ml/train_model.py <topic>     # 예: python ml/train_model.py realestate
    python ml/train_model.py             # 데이터가 있는 토픽 전부

산출물:
    ml/models/<topic>.pkl   학습된 파이프라인 + 운영 컷오프(메타)
    ml/reports/<topic>.txt  out-of-fold 성능 리포트
"""

import json
import os
import pickle
import sys

import numpy as np
from sklearn.model_selection import GroupKFold
from sklearn.metrics import (classification_report, f1_score, precision_score,
                             recall_score, roc_auc_score)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TOPICS  # noqa: E402
from ml.relevance import RelevanceModel, build_text  # noqa: E402

ML_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ML_DIR, "models")
REPORTS_DIR = os.path.join(ML_DIR, "reports")
DATA_DIR = os.path.join(ML_DIR, "data")

# 최소 데이터 요건 — 이보다 적으면 학습해도 신뢰할 수 없다.
MIN_PER_CLASS = 150

# 라벨 신뢰도별 학습 가중치.
# 같은 기사를 독립적으로 두 번 라벨링해 일치도를 재보니 확신도별로 크게 갈렸다
# (realestate 전체 92% / high 99% / medium 88% / low 65%, sto 전체 86% / low 55%).
# low 라벨을 1.0 으로 넣으면 잡음을 그대로 학습한다. 'ambiguous' 는 두 라벨러가
# 실제로 엇갈린 건이라 가장 낮게 준다.
# 'split' = 3표 재투표에서 2:1 로 갈린 건. 진짜 경계 사례라 절반만 반영한다.
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "split": 0.5, "low": 0.3, "ambiguous": 0.15}

# 토픽별 학습 설정. GroupKFold out-of-fold F1 기준으로 탐색한 결과를 고정한 값이다
# (2026-08-31, realestate 2000건 / sto 900건).
#
# use_rule_score=False 인 이유: 가중치 점수를 특징으로 넣으면 **오히려 나빠졌다**
# (sto F1 0.800 → 0.783, realestate 는 동률). 점수가 보는 신호는 이미 텍스트 안에 있고,
# 규칙의 편향까지 같이 들어오기 때문이다. 덕분에 추론 인터페이스도 텍스트만으로 단순해진다.
DEFAULT_CFG = {
    "title_weight": 2,          # 제목을 몇 번 반복해 가중할지 (라벨러도 제목을 주근거로 삼는다)
    "char_ngram": (2, 5),
    "char_max": 60000,
    "word_ngram": (1, 2),
    "word_max": 40000,
    "min_df": 2,
    "use_rule_score": False,
    "C": 3.0,
}
TOPIC_CFG = {
    # realestate: char(1,4) 가 근소 우위
    "realestate": {"char_ngram": (1, 4), "C": 3.0},
    # sto: 데이터가 적어(1,139건) 규제를 더 건다
    "sto": {"char_ngram": (2, 5), "C": 1.0},
}

# 본문 모델 설정 (body_chars > 0). 본문 입력에 맞춰 따로 탐색한 값이다.
# realestate 는 본문이 실익이 있고(AUC 0.889 → 0.912), sto 는 거의 없어서(+0.005)
# 굳이 매 주기 네트워크 요청을 늘릴 이유가 없다 → realestate 만 만든다.
BODY_CFG = {
    "realestate": {"char_ngram": (1, 4), "word_ngram": (1, 2), "C": 10.0,
                   "char_max": 80000, "word_max": 60000, "body_chars": 1500},
}


def _cfg(topic, body=False):
    cfg = {**DEFAULT_CFG, **TOPIC_CFG.get(topic, {})}
    if body:
        if topic not in BODY_CFG:
            return None
        cfg = {**cfg, **BODY_CFG[topic]}
    return cfg


def _load_bodies(topic):
    """ml/data/<topic>_bodies.jsonl — 기사 본문 사이드카.

    본문은 언론사 기사 원문이라 저장소에 넣지 않는다(.gitignore). 없으면 본문 모델만
    학습하지 못하고 스니펫 모델은 정상 동작한다. 다시 모으려면 원문 URL 로 재수집하면 된다.
    """
    path = os.path.join(DATA_DIR, f"{topic}_bodies.jsonl")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("url") and d.get("body"):
                out[d["url"]] = d["body"]
    return out


def load_data(topic):
    """ml/data/<topic>.jsonl 에서 학습 데이터를 읽는다 (본문은 사이드카에서 합친다)."""
    path = os.path.join(DATA_DIR, f"{topic}.jsonl")
    if not os.path.exists(path):
        return []
    bodies = _load_bodies(topic)
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "y" not in d or not d.get("title"):
                continue
            rows.append({
                "title": d["title"],
                "description": d.get("description") or "",
                "y": int(d["y"]),
                "group": d.get("group") or d.get("url") or d["title"],
                "rule_score": float(d.get("rule_score") or 0.0),
                "confidence": d.get("confidence") or "medium",
                "body": d.get("body") or bodies.get(d.get("url"), ""),
            })
    return rows


def _weights(rows):
    return np.array([CONFIDENCE_WEIGHT.get(r.get("confidence"), 0.7) for r in rows])


def _oof(rows, cfg, n_splits=5):
    """out-of-fold 확률 — near-duplicate 누수를 GroupKFold 로 막는다."""
    y = np.array([r["y"] for r in rows])
    groups = np.array([r["group"] for r in rows])
    bc = cfg.get("body_chars", 0)
    texts = [build_text(r["title"], r["description"], cfg["title_weight"], r.get("body", ""), bc)
             for r in rows]
    scores = [r["rule_score"] for r in rows]

    sw = _weights(rows)
    n_splits = min(n_splits, len(set(groups)))
    prob = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=n_splits).split(texts, y, groups):
        m = RelevanceModel(cfg).fit([texts[i] for i in tr], [scores[i] for i in tr],
                                    y[tr], sample_weight=sw[tr])
        prob[te] = m.predict_proba([texts[i] for i in te], [scores[i] for i in te])[:, 1]
    return y, prob


def _pick_cutoff(y, prob, objective="f1", min_precision=None):
    """운영 컷오프 선택. min_precision 을 주면 그 정밀도를 만족하는 것 중 재현율 최대."""
    best = (0.0, 0.5)
    for c in np.arange(0.20, 0.90, 0.01):
        pred = (prob >= c).astype(int)
        if pred.sum() == 0:
            continue
        p = precision_score(y, pred, zero_division=0)
        r = recall_score(y, pred, zero_division=0)
        if min_precision is not None:
            if p >= min_precision and r > best[0]:
                best = (r, c)
        else:
            f = f1_score(y, pred, zero_division=0)
            if f > best[0]:
                best = (f, c)
    return float(best[1])


def train(topic, min_precision=None, body=False):
    if topic not in TOPICS:
        print(f"[{topic}] 알 수 없는 토픽 — 건너뜀")
        return
    cfg = _cfg(topic, body=body)
    if cfg is None:
        return
    tag = "_body" if body else ""
    rows = load_data(topic)
    if body:
        # 본문이 있는 건으로만 학습한다. 본문 없는 건을 섞으면 본문 특징이 희석된다.
        rows = [r for r in rows if len(r.get("body") or "") > 200]
        if not rows:
            print(f"[{topic}_body] 본문 사이드카(ml/data/{topic}_bodies.jsonl)가 없어 건너뜀")
            return
    if not rows:
        print(f"[{topic}] 학습 데이터 없음 — ml/data/{topic}.jsonl 을 먼저 만드세요.")
        print(f"        (라벨은 반드시 키워드 규칙과 독립적으로 만들 것 — 모듈 상단 설명 참고)")
        return

    pos = sum(r["y"] for r in rows)
    neg = len(rows) - pos
    ngroup = len(set(r["group"] for r in rows))
    print(f"[{topic}{tag}] 데이터 {len(rows)}건 (적합 {pos} / 부적합 {neg}), 그룹 {ngroup}개")
    if pos < MIN_PER_CLASS or neg < MIN_PER_CLASS:
        print(f"[{topic}{tag}] 데이터 부족 (클래스당 {MIN_PER_CLASS}건 이상 필요). 학습 중단.")
        return

    y, prob = _oof(rows, cfg)
    cutoff = _pick_cutoff(y, prob, min_precision=min_precision)
    pred = (prob >= cutoff).astype(int)

    f1 = f1_score(y, pred, zero_division=0)
    prec = precision_score(y, pred, zero_division=0)
    rec = recall_score(y, pred, zero_division=0)
    auc = roc_auc_score(y, prob) if len(set(y)) > 1 else float("nan")

    print(f"[{topic}{tag}] out-of-fold  F1 {f1:.3f}  정밀도 {prec:.1%}  재현율 {rec:.1%}  AUC {auc:.3f}  (컷오프 {cutoff:.2f})")

    # 운영 지점표 — 컷오프를 나중에 바꿀 때 참고용
    table = []
    for c in [0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        p = (prob >= c).astype(int)
        table.append((c, precision_score(y, p, zero_division=0),
                      recall_score(y, p, zero_division=0),
                      f1_score(y, p, zero_division=0), p.mean()))

    # 전체 데이터로 최종 학습
    texts = [build_text(r["title"], r["description"], cfg["title_weight"],
                        r.get("body", ""), cfg.get("body_chars", 0)) for r in rows]
    scores = [r["rule_score"] for r in rows]
    model = RelevanceModel(cfg).fit(texts, scores, y, sample_weight=_weights(rows))
    model.cutoff = cutoff
    model.metrics = {"f1": f1, "precision": prec, "recall": rec, "auc": auc,
                     "n": len(rows), "pos": int(pos), "neg": int(neg)}

    os.makedirs(MODELS_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(os.path.join(MODELS_DIR, f"{topic}{tag}.pkl"), "wb") as f:
        pickle.dump(model, f)

    with open(os.path.join(REPORTS_DIR, f"{topic}{tag}.txt"), "w", encoding="utf-8") as f:
        f.write(f"[{topic}{tag}] 데이터 {len(rows)}건 (적합 {pos} / 부적합 {neg}), 그룹 {ngroup}개\n")
        f.write("평가는 전부 out-of-fold (GroupKFold) — in-sample 수치 아님\n\n")
        f.write(f"F1 {f1:.3f} / 정밀도 {prec:.1%} / 재현율 {rec:.1%} / AUC {auc:.3f}\n")
        f.write(f"운영 컷오프 {cutoff:.2f}\n\n")
        f.write(classification_report(y, pred, target_names=["부적합", "적합"], zero_division=0))
        f.write("\n운영 지점표 (확률 컷오프별)\n")
        f.write(f"{'cut':>6}{'정밀도':>10}{'재현율':>10}{'F1':>8}{'통과비율':>10}\n")
        for c, p, r, ff, rate in table:
            f.write(f"{c:>6.2f}{p:>10.1%}{r:>10.1%}{ff:>8.3f}{rate:>10.1%}\n")
        f.write(f"\n설정: {json.dumps(cfg, ensure_ascii=False)}\n")

    print(f"[{topic}{tag}] 모델 저장: ml/models/{topic}{tag}.pkl  (리포트: ml/reports/{topic}{tag}.txt)")


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    minp = None
    for a in sys.argv[1:]:
        if a.startswith("--min-precision="):
            minp = float(a.split("=", 1)[1])
    for t in (argv or list(TOPICS)):
        train(t, min_precision=minp)
        if t in BODY_CFG:
            train(t, min_precision=minp, body=True)
