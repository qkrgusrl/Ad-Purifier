"""분류기 비교: 룰 점수 vs Laya(제로샷/파인튜닝).

- eval_set.jsonl : 손으로 작성한 검증셋 60문장
- dev.jsonl      : make_dataset.py가 학습에 안 쓴 어휘로 만든 문장 (외운 키워드가 아닌 일반화 성능)
- real_cases.jsonl: 팀원 조사 URL 28개에서 뽑은 실제 문구(정상 10·광고 46). 학습에 쓰지 않는 평가 전용 — 어려운 정상(예방교육·통계·인용)과 실제 스팸
각각 전체 성능과, 실제로 Laya가 판정하는 '경계 구간'(룰 점수 LOW~HIGH) 성능을 따로 낸다.

사용: python training/eval_classifier.py [모델경로 ...]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from detector.adscore import HIGH, score, verdict  # noqa: E402
from detector.textnorm import normalize  # noqa: E402
from training.make_dataset import QUESTION  # noqa: E402

HERE = Path(__file__).resolve().parent


def load(name):
    rows = [json.loads(l) for l in (HERE / name).read_text(encoding="utf-8").splitlines() if l.strip()]
    if rows and "state" in rows[0]:
        rows = [{"text": r["state"], "label": r["expected"]["is_illegal_ad"]} for r in rows]
    texts = [normalize(r["text"]) for r in rows]
    return texts, [r["label"] for r in rows]


SETS = {name: load(name) for name in ("eval_set.jsonl", "dev.jsonl", "real_cases.jsonl") if (HERE / name).exists()}


def report(name, pred, gold, mask=None):
    idx = [i for i in range(len(gold)) if mask is None or mask[i]]
    if not idx:
        return
    tp = sum(pred[i] and gold[i] for i in idx)
    fp = sum(pred[i] and not gold[i] for i in idx)
    fn = sum(gold[i] and not pred[i] for i in idx)
    acc = sum(pred[i] == gold[i] for i in idx) / len(idx)
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    print(f"  {name:30} n={len(idx):3} acc={acc:.3f} precision={prec:.3f} recall={rec:.3f} (TP={tp} FP={fp} FN={fn})")


rule_scores = {s: [score(t)[0] for t in texts] for s, (texts, _) in SETS.items()}
border = {s: [verdict(x) == "borderline" for x in xs] for s, xs in rule_scores.items()}
for s, (texts, gold) in SETS.items():
    print(f"[{s}]")
    report("rules (score>=HIGH)", [x >= HIGH for x in rule_scores[s]], gold)

from laya.agent import Agent  # noqa: E402

for model in sys.argv[1:] or ["convaiinnovations/laya-multilingual"]:
    agent = Agent(model, device="cpu")
    agent.predict_batch(["워밍업"], QUESTION)
    print(f"\n=== laya {Path(model).name}")
    for s, (texts, gold) in SETS.items():
        t0 = time.perf_counter()
        out = agent.predict_batch(texts, QUESTION, batch_size=16)
        dt = (time.perf_counter() - t0) / len(texts) * 1000
        probs = [o["answers"]["is_illegal_ad"]["noul"] for o in out]
        pred = [p >= 0.5 for p in probs]
        print(f"[{s}] CPU {dt:.1f} ms/문장 (batch 16)")
        report("laya 전체", pred, gold)
        report("laya 경계 구간만", pred, gold, border[s])
        # 파이프라인(detector.main.judge와 같은 규칙): 확정 구간은 룰, 경계 구간만 Laya
        pipe = [p if b else verdict(x) == "ad" for p, b, x in zip(pred, border[s], rule_scores[s])]
        report("룰 + laya(경계만)", pipe, gold)
        if s in ("eval_set.jsonl", "real_cases.jsonl"):
            for t, p, g, pr in zip(texts, pred, gold, probs):
                if p != g:
                    print(f"     오분류 gold={g!s:5} p={pr:.2f} | {t}")
