"""분류기 비교: 룰 점수 vs Laya(제로샷/파인튜닝). 손으로 작성한 eval_set.jsonl 기준.

사용: python training/eval_classifier.py [모델경로 ...]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from detector.adscore import HIGH, score  # noqa: E402
from detector.textnorm import normalize  # noqa: E402
from training.make_dataset import QUESTION  # noqa: E402

rows = [json.loads(l) for l in Path(__file__).with_name("eval_set.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
texts = [normalize(r["text"]) for r in rows]
gold = [r["label"] for r in rows]


def report(name, pred):
    tp = sum(p and g for p, g in zip(pred, gold))
    fp = sum(p and not g for p, g in zip(pred, gold))
    fn = sum(g and not p for p, g in zip(pred, gold))
    acc = sum(p == g for p, g in zip(pred, gold)) / len(gold)
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    print(f"{name:28} acc={acc:.3f} precision={prec:.3f} recall={rec:.3f} (TP={tp} FP={fp} FN={fn})")
    return [(t, g) for t, p, g in zip(texts, pred, gold) if p != g]


report("rules (score>=HIGH)", [score(t)[0] >= HIGH for t in texts])

from laya.agent import Agent  # noqa: E402

for model in sys.argv[1:] or ["convaiinnovations/laya-multilingual"]:
    agent = Agent(model, device="cpu")
    agent.predict_batch(texts[:2], QUESTION)  # warm-up
    t0 = time.perf_counter()
    out = agent.predict_batch(texts, QUESTION, batch_size=16)
    dt = (time.perf_counter() - t0) / len(texts) * 1000
    probs = [o["answers"]["is_illegal_ad"]["noul"] for o in out]
    errs = report(f"laya {Path(model).name}", [p >= 0.5 for p in probs])
    print(f"   CPU {dt:.1f} ms/문장 (batch 16)")
    for t, g in errs:
        print(f"   오분류 gold={g!s:5} p={probs[texts.index(t)]:.2f} | {t}")
