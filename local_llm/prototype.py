"""v15 검증/추출 envelope 및 원문 결합."""
from .engine import resolve_sentence_choice
from .prompts import input_only, verification_token_limit

def plan_chunks(server, blocks, version, max_tokens=512, preflight_errors=None):
    """v15는 총 후보 제한 없이 각 블록을 그대로 사전 검사한다."""
    if version != 'v15':
        raise ValueError('v15만 지원')
    chunks, oversized = [], []
    for block in blocks:
        try:
            inp = {'candidate_id': block['block_id'], 'html': block['html'], 'context': block.get('context', '')}
            if server.prompt_size('verify', inp, version) + verification_token_limit(version) + 64 <= server.ctx_size:
                chunks.append([block])
            else:
                oversized.append(block['block_id'])
        except Exception as exc:
            if preflight_errors is None:
                raise
            preflight_errors.append({'block_ids': [block['block_id']],
                                     'error': f'preflight_error: {type(exc).__name__}: {exc}',
                                     'action': '원문과 오류를 보존해 재검토 또는 사전 검사 재시도'})
    return chunks, oversized


def bind_candidates(blocks, candidates, bindings=None):
    sources = {b['block_id']: b for b in blocks}
    if len(sources) != len(blocks):
        raise ValueError('중복 원문 owner')
    bound = []
    fields = ('evidence_id', 'owner_id', 'source_field', 'start_char', 'end_char', 'source_sha256')
    for candidate in candidates:
        owner = candidate['block_id']
        source, binding = sources.get(owner), (bindings or {}).get(owner)
        if source is None or not binding:
            raise ValueError('missing_source_binding: v15 근거 ID 필요')
        expected = resolve_sentence_choice({'candidate_id': owner, 'html': source['html'], 'context': source.get('context', '')},
                                           {'verdict': 'ad', 'evidence_id': binding.get('evidence_id')}, 'v15')
        if any(binding.get(key) != expected[key] for key in fields) or candidate.get('evidence') != expected['evidence']:
            raise ValueError('stale_source_binding: 원문 참조 불일치')
        bound.append({**candidate, **{key: expected[key] for key in fields}})
    return bound


def run_extract(server, data, version):
    clean = input_only("extract", data)
    blocks = clean["blocks"]
    ids = [b["block_id"] for b in blocks]
    if len(set(ids)) != len(ids):
        raise ValueError("block_id는 입력 전체에서 고유해야 함")
    calls, candidates, failures = [], [], []
    chunks, oversized = plan_chunks(server, blocks, version, preflight_errors=failures)
    failures.extend({"block_ids": [bid], "error": "input_overflow", "action": "상위 파서에서 문맥을 보존해 더 작은 블록으로 분할"} for bid in oversized)

    def process(chunk):
        result = server.infer("extract", {"blocks": chunk}, version)
        calls.append(result)
        candidates.extend(bind_candidates(chunk, result.get("validation", {}).get("accepted", []),result.get('source_bindings')))
        if result.get("unresolved_block_ids"):
            failures.append({"block_ids": result["unresolved_block_ids"], "error": "uncertain", "action": "문맥 추가 수집 또는 재검토 필요"})
        if result["status"] != "ok":
            failures.append({"block_ids": [b["block_id"] for b in chunk], "error": result.get("error", result.get("validation", {}).get("errors")),
                             "action": "재검토 필요; 미검출이나 정상으로 확정하지 않음"})

    for chunk in chunks:
        process(chunk)
    return {"task": "extract", "version": version, "status": "needs_review" if failures else "ok",
            "input_block_count": len(blocks), "candidates": candidates, "failures": failures, "calls": calls,
            "next_step": "후보 원문과 참조를 Laya 판정 흐름으로 전달"}


def run_verify(server, data, version):
    inputs = data.get("candidates", [data])
    ids = [item["candidate_id"] for item in inputs]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate_id는 입력 전체에서 고유해야 함")
    results = []
    for item in inputs:
        call = server.infer("verify", item, version)
        verdict = call.get("parsed", {}).get("verdict") if call["status"] == "ok" else "uncertain"
        results.append({"candidate_id": item["candidate_id"], "status": call["status"], "verdict": verdict,
                        "evidence": call.get("parsed", {}).get("evidence") if call["status"] == "ok" else None,
                        "needs_review": verdict == "uncertain", "call": call})
        if call['status']=='ok' and 'evidence_id' in call['parsed']:
            results[-1].update({k:call['parsed'][k] for k in ('evidence_id','source_field','start_char','end_char','source_sha256','evidence_scope')})
            if 'owner_id' in call['parsed']:
                results[-1]['owner_id']=call['parsed']['owner_id']
    return {"task": "verify", "version": version, "status": "needs_review" if any(r["needs_review"] for r in results) else "ok", "results": results}
