"""게시 적합성 모델의 정의 — 학습(train_model.py)과 추론(classifier.py)이 공유한다.

## 왜 별도 모듈인가

모델 객체를 pickle 로 저장하면 클래스의 **import 경로가 같이 박힌다.** 이 클래스를
train_model.py 안에 두고 `python ml/train_model.py` 로 학습하면 `__main__.RelevanceModel`
로 저장되고, 나중에 classifier.py 에서 읽을 때 `Can't get attribute 'RelevanceModel' on
<module '__main__'>` 로 터진다. 여기 두면 학습 방식과 무관하게 항상 `ml.relevance.RelevanceModel`
로 저장·복원된다.
"""

import numpy as np
from scipy.sparse import hstack, csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression


def build_text(title: str, description: str, title_weight: int = 2,
               body: str = "", body_chars: int = 0) -> str:
    """제목을 반복해 가중하고, 본문이 있으면 뒤에 붙인다.

    body_chars=0 이면 본문을 쓰지 않는다(스니펫 모델). 본문 모델은 1500자를 쓴다 —
    실측상 1000~2000자 구간에서 AUC 가 평평해서 그 안쪽이면 어디든 무방하다.
    """
    t = (title or "").strip()
    d = (description or "").strip()
    out = ((t + " ") * title_weight) + d
    if body_chars and body:
        out += " " + body.strip()[:body_chars]
    return out


class RelevanceModel:
    """벡터라이저 + 분류기 + 운영 컷오프를 한 덩어리로 들고 다닌다.

    `use_rule_score` 는 기본적으로 끈다 — 가중치 점수를 특징으로 넣으면 성능이 오히려
    떨어졌다(sto F1 0.800 → 0.783). 점수가 보는 신호는 이미 텍스트 안에 있고, 규칙의
    편향까지 같이 들어오기 때문이다.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.char_vec = None
        self.word_vec = None
        self.clf = None
        self.cutoff = 0.5
        self.score_mu = 0.0
        self.score_sd = 1.0
        self.metrics = {}

    # ── 내부 ──
    def _blocks(self, texts, scores, fit=False):
        blocks = []
        if self.cfg.get("char_ngram"):
            if fit:
                self.char_vec = TfidfVectorizer(
                    analyzer="char_wb", ngram_range=tuple(self.cfg["char_ngram"]),
                    max_features=self.cfg["char_max"], min_df=self.cfg["min_df"],
                    sublinear_tf=True)
                blocks.append(self.char_vec.fit_transform(texts))
            else:
                blocks.append(self.char_vec.transform(texts))
        if self.cfg.get("word_ngram"):
            if fit:
                self.word_vec = TfidfVectorizer(
                    analyzer="word", ngram_range=tuple(self.cfg["word_ngram"]),
                    max_features=self.cfg["word_max"], min_df=self.cfg["min_df"],
                    sublinear_tf=True)
                blocks.append(self.word_vec.fit_transform(texts))
            else:
                blocks.append(self.word_vec.transform(texts))
        if self.cfg.get("use_rule_score"):
            s = np.asarray(scores, dtype=float).reshape(-1, 1)
            if fit:
                self.score_mu = float(s.mean())
                self.score_sd = float(s.std()) or 1.0
            blocks.append(csr_matrix((s - self.score_mu) / self.score_sd))
        return hstack(blocks).tocsr()

    # ── 학습/추론 ──
    def fit(self, texts, scores, y, sample_weight=None):
        X = self._blocks(texts, scores, fit=True)
        self.clf = LogisticRegression(max_iter=4000, class_weight="balanced",
                                      C=self.cfg["C"])
        self.clf.fit(X, y, sample_weight=sample_weight)
        return self

    def predict_proba(self, texts, scores=None):
        if scores is None:
            scores = [self.score_mu] * len(texts)
        return self.clf.predict_proba(self._blocks(texts, scores, fit=False))

    def probability(self, title: str, description: str, rule_score=None,
                    body: str = "") -> float:
        """기사 1건의 게시 적합 확률 (0.0~1.0)."""
        txt = build_text(title, description, self.cfg.get("title_weight", 2),
                         body, self.cfg.get("body_chars", 0))
        sc = [rule_score if rule_score is not None else self.score_mu]
        return float(self.predict_proba([txt], sc)[0][1])
