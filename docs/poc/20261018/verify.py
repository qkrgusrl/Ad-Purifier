"""저장된 PoC 증거를 표준 라이브러리로 검증한다. 모델/네트워크 호출과 파일 쓰기는 없다."""
import hashlib
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def verify():
    metadata = read("metadata.json")
    for name, expected in read("checksums.json").items():
        assert digest((HERE / name).read_bytes()) == expected, ("file_hash", name)
    cases = read("inputs.json")
    routes = read("routes.json")
    laya = read("laya.json")
    gemma = read("gemma.json")
    gold = {c["id"]: c["label"] for c in cases}
    by_id = {c["id"]: c for c in cases}
    assert len(gold) == len(cases) == 24
    assert len(routes) == 24 and {r["id"] for r in routes} == set(gold)
    lr = {r["id"]: r for r in laya["rows"]}
    gr = {r["id"]: r for r in gemma}
    assert len(lr) == len(laya["rows"]) == len(gr) == len(gemma) == 11
    assert set(lr) == set(gr) == {r["id"] for r in routes if r["route"] == "laya"}
    assert len(laya["states"]) == len(laya["outputs"]) == 8
    assert digest(json.dumps(laya["question"], ensure_ascii=False, sort_keys=True).encode()) == laya["question_sha256"]
    assert digest((HERE / "prompt-v15.txt").read_bytes()) == metadata["gemma"]["prompt_sha256"]
    assert metadata["frozen_at"] < metadata["execution"]["started_at"]
    assert metadata["execution"]["laya_wait_completed_at"] < metadata["gemma"]["started_at"]
    lc = gc = low = input_tokens = output_tokens = errors = 0
    for cid, row in lr.items():
        raw = row["raw"]
        assert raw == laya["outputs"][row["index"]]
        assert row["state"] == laya["states"][row["index"]]
        answer = raw["answers"]["is_illegal_ad"]
        assert row["p_true"] == answer["noul"] and row["confidence"] == answer["confidence"]
        verdict = "ad" if round(answer["noul"], 4) >= 0.5 else "clean"
        assert row["verdict"] == verdict and row["status"] == "ok"
        assert row["usage"] == raw["usage"]
        assert not raw["usage"]["truncated"] and raw["usage"]["state_tokens_dropped"] == 0
        lc += verdict == gold[cid]
        low += answer["confidence"] < 0.8
        assert row["gemma_route"] == "off_policy_diagnostic"
    for cid, row in gr.items():
        result = row["result"]
        assert row["gemma_route"] == "off_policy_diagnostic"
        source = result["input"]
        assert source == {"candidate_id": cid, "html": by_id[cid]["html"], "context": by_id[cid]["context"]}
        parsed = result["parsed"]
        raw = result["raw_response"]
        choice = raw["choices"][0]
        model = json.loads(choice["message"]["content"])
        assert choice["finish_reason"] == "stop"
        assert model == result["model_output"]
        assert model["verdict"] == parsed["verdict"]
        assert model["evidence_id"] == parsed["evidence_id"]
        assert parsed["owner_id"] == cid
        assert parsed["evidence"] == source[parsed["source_field"]][parsed["start_char"]:parsed["end_char"]]
        bound_hash = digest(json.dumps({k: source[k] for k in ("html", "context")}, ensure_ascii=False, sort_keys=True).encode())
        assert parsed["source_sha256"] == bound_hash
        assert any(u["id"] == parsed["evidence_id"] and u["selectable"] for u in result["model_input"]["source_units"])
        assert result["prompt_sha256"] == metadata["gemma"]["prompt_sha256"]
        assert not result["validation"]["errors"]
        gc += model["verdict"] == gold[cid]
        errors += result["status"] != "ok"
        input_tokens += raw["usage"]["prompt_tokens"]
        output_tokens += raw["usage"]["completion_tokens"]
    for cid, signal in (("S06", "page-context"), ("S07", "body-context")):
        row = next(r for r in routes if r["id"] == cid)
        assert row["route"] == "context_bypass" and row["target"]["score"] == 2
        assert row["target"]["verdict"] == "borderline"
        assert [signal, -4] in row["target"]["context"]
        assert "ml_prob" not in row["target"] and cid not in lr and cid not in gr
    summary = {
        "cases": len(cases), "laya_candidates": len(lr), "laya_unique_inputs": len(laya["states"]),
        "laya_three_class_matches": lc, "gemma_diagnostic_matches": gc,
        "initial_low_confidence_count": low, "off_policy_gemma_calls": len(gr),
        "route_counts": dict(Counter(r["route"] for r in routes)),
        "gemma_input_tokens": input_tokens, "gemma_output_tokens": output_tokens,
        "gemma_errors": errors, "unlabeled_real_cases": sum(v is None for v in gold.values()),
    }
    assert (summary["cases"], len(lr), lc, gc) == (24, 11, 7, 8)
    assert summary == read("measured-summary.json"), "stored_summary_mismatch"
    print(json.dumps({"status": "PASS", "measured": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    verify()
