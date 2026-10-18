"""규칙 경계 검증과 수집된 DOM 잔여 회수. 실모델은 필요한 단계에서만 연다."""
import hashlib
import html
import json
import math
import time
from contextlib import ExitStack
from html.parser import HTMLParser

from .textnorm import normalize
from .laya_runtime import LayaCleanupError, LayaRunner


def digest(item):
    return hashlib.sha256(json.dumps({k: item.get(k, "") for k in ("html", "context")},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def valid_binding(row, item, owner):
    """모델 문구가 실제 요청 소유 단위의 원문 범위에 결합되는지 확인한다."""
    field, start, end = row.get("source_field"), row.get("start_char"), row.get("end_char")
    return (row.get("owner_id") == owner and row.get("source_sha256") == digest(item)
            and isinstance(row.get("evidence_id"), str) and field in ("html", "context")
            and type(start) is int and type(end) is int
            and 0 <= start < end <= len(item.get(field, ""))
            and item[field][start:end] == row.get("evidence"))


class _OwnerText(HTMLParser):
    """하위 요소의 소유 관계를 추측하지 않고 단일 요소의 본문만 읽는다."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag = None
        self.closed = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.tag is not None or tag in ("script", "style", "title", "textarea", "template", "noscript"):
            raise ValueError("본문 소유가 불명확한 HTML")
        self.tag = tag

    def handle_endtag(self, tag):
        if self.tag != tag or self.closed:
            raise ValueError("단일 요소가 아닌 HTML")
        self.closed = True

    def handle_data(self, data):
        if self.tag is None or self.closed:
            if data.strip():
                raise ValueError("소유 요소 밖의 본문")
        else:
            self.parts.append(data)

    def handle_comment(self, data):
        raise ValueError("주석 포함 근거는 재검토")

    def handle_decl(self, decl):
        raise ValueError("선언 포함 근거는 재검토")

    def handle_pi(self, data):
        raise ValueError("처리 지시 포함 근거는 재검토")


def matching_recovery_text(evidence, row):
    expected = normalize(row["text"])
    if normalize(html.unescape(evidence)) == expected:
        return True  # 기존 태그 없는 원문 binding 호환.
    if evidence != row.get("source_html") or row.get("attr"):
        return False
    parser = _OwnerText()
    try:
        parser.feed(evidence)
        parser.close()
    except ValueError:
        return False
    return (parser.closed and parser.tag == row.get("tag", "").lower()
            and normalize("".join(parser.parts)) == expected)


def analyze(cands, pages, model_path, use_ml, deadline=None, cancel=None, laya_confidence_threshold=.8, cancel_error=None, detect_posted=True):
    from .main import ScanCancelled, candidates_from, judge, source_fields
    ScanCancelled = cancel_error or ScanCancelled
    if not .5 <= laya_confidence_threshold <= 1:
        raise ValueError("laya_confidence_threshold는 0.5~1 범위여야 합니다")
    stats = {"mode": "policy" if use_ml else "diagnostic", "laya_confidence_threshold": laya_confidence_threshold,
             "laya_inputs": 0, "gemma_verify": 0, "gemma_extract": 0, "residual_screened_clean": 0,
             "unresolved_sources": [], "failures": [], "source_results": [],
             "scope": "수집된 DOM 텍스트 소유 단위; 전체 HTTP 원문 및 이미지 검사는 포함하지 않음"}

    def check():
        if cancel is not None and cancel.is_set():
            raise ScanCancelled()
        return deadline is None or time.perf_counter() < deadline

    def review(c, reason, status="review"):
        c.update(verdict="uncertain", processing_status=status, reason=reason)

    def unresolved(row, reason, status="review"):
        stats["unresolved_sources"].append({**source_fields(row), "selector": row.get("selector"),
                                            "url": row.get("url"), "reason": str(reason), "processing_status": status})

    def about(c):
        return any(n in ("page-context", "body-context") for n, _ in c.get("context", []))

    def input_for(row):
        return {"html": row.get("source_html", ""), "context": row.get("source_context", "")}

    # 같은 문구라도 다른 원문/주변 문맥이면 별도 예측. 같은 source의 회수 후 판정만 캐시 재사용.
    def key(row):
        return (normalize(row.get("evidence_text", row.get("text", ""))), digest(input_for(row)))

    check()
    used = {c.get("source_id") for c in cands}
    residual, seen = [], set()
    for url, frames in pages:
        for prefix, data in frames:
            for row in data.get("source_blocks", []):
                sid = row.get("source_id")
                if sid and sid not in used and sid not in seen:
                    seen.add(sid)
                    residual.append({**row, "url": url, "prefix": prefix})
    border = []
    for c in cands:
        if c["verdict"] == "borderline":
            border.append(c)
            review(c, "AI 판정 대기")
        else:
            c.update(processing_status="completed", reason="기존 규칙 확정")

    if not use_ml:
        for c in border:
            review(c, "명시적 진단 모드: AI 판정 생략")
        for row in residual:
            unresolved(row, "명시적 진단 모드: 잔여 선별 생략")
        return stats

    predictions = {}
    rows_by_key = {key(c): c for c in border if not about(c)}
    rows_by_key.update((key(r), r) for r in residual)
    try:
        if rows_by_key and check():
            from training.make_dataset import QUESTION
            with LayaRunner(model_path, deadline=deadline, cancel=cancel) as runner:
                keys = sorted(rows_by_key)
                for offset in range(0, len(keys), 64):
                    if not check():
                        break
                    chunk = keys[offset:offset + 64]
                    stats["laya_inputs"] += len(chunk)
                    try:
                        outputs = runner.predict_batch([k[0] for k in chunk], QUESTION, batch_size=16)
                        if not check():
                            break
                        for index, k in enumerate(chunk):
                            try:
                                out = outputs[index]
                                p = float(out["answers"]["is_illegal_ad"]["noul"])
                                usage = out.get("usage") or {}
                                if (out.get("truncated") or usage.get("truncated") or usage.get("state_tokens_dropped", 0)
                                        or usage.get("truncated_questions")
                                        or out.get("status", "ok") not in ("ok", "completed")):
                                    raise ValueError("잘린 또는 미완료 Laya 출력")
                                if not math.isfinite(p) or not 0 <= p <= 1:
                                    raise ValueError("잘못된 Laya 확률")
                                predictions[k] = p
                            except (IndexError, KeyError, TypeError, ValueError):
                                stats["failures"].append({"source_id": rows_by_key[k].get("source_id"), "stage": "laya", "reason": "출력 누락/잘림/오류"})
                    except LayaCleanupError:
                        raise
                    except Exception as exc:
                        check()
                        stats["failures"].extend({"source_id": rows_by_key[k].get("source_id"), "stage": "laya", "reason": str(exc)} for k in chunk)
    except LayaCleanupError as exc:
        # __enter__ 실패 때도 PID/정리 단계가 포함된 전용 타입으로 Gemma 시작을 차단한다.
        stats["failures"].extend({"source_id": r.get("source_id"), "stage": "laya_cleanup", "reason": str(exc)}
                                 for r in rows_by_key.values())
        for c in border:
            review(c, str(exc), "error")
        for row in residual:
            unresolved(row, str(exc), "error")
        return stats
    except Exception as exc:
        check()
        stats["failures"].extend({"source_id": r.get("source_id"), "stage": "laya", "reason": str(exc)} for r in rows_by_key.values())
    check()

    pending = []
    def route(c):
        if not c.get("source_html"):
            review(c, "원본 HTML 없음: 재합성하지 않음")
            stats["failures"].append({"source_id": c.get("source_id"), "stage": "source", "reason": c["reason"]})
            return
        p = None if about(c) else predictions.get(key(c))
        if p is not None:
            c["ml_prob"] = round(p, 4)
            c["laya_confidence"] = max(p, 1 - p)
        if p is not None and max(p, 1 - p) >= laya_confidence_threshold:
            c.update(verdict="ad" if p >= .5 else "clean", decided_by="laya",
                     processing_status="completed", reason="Laya 정상 출력의 confidence가 운영 임계값 이상")
        else:
            pending.append(c)
    for c in border:
        route(c)

    extract_rows = []
    for row in residual:
        p = predictions.get(key(row))
        if not row.get("source_html"):
            unresolved(row, "원본 HTML 없음: 재합성하지 않음")
        elif p is not None and p < .5 and max(p, 1 - p) >= laya_confidence_threshold:
            stats["residual_screened_clean"] += 1
            stats["source_results"].append({"source_id": row["source_id"], "verdict": "clean", "decided_by": "laya"})
        else:
            extract_rows.append(row)

    with ExitStack() as stack:
        client = None
        client_error = None
        def get_client():
            nonlocal client, client_error
            if not check():
                raise TimeoutError("공유 점검 시간 예산 초과")
            if client_error:
                raise RuntimeError(client_error)
            if client is None:
                try:
                    from local_llm.client import GemmaClient
                    client = stack.enter_context(GemmaClient(deadline=deadline, cancel=cancel))
                except Exception as exc:
                    client_error = str(exc)
                    raise
            return client

        if extract_rows:
            blocks = [{"block_id": r["source_id"], **input_for(r)} for r in extract_rows]
            by_id = {r["source_id"]: r for r in extract_rows}
            try:
                gemma = get_client()
                stats["gemma_extract"] += len(blocks)
                response = gemma.extract({"blocks": blocks})
                if not check():
                    raise TimeoutError("Gemma 추출 중 공유 시간 예산 초과")
                stats["extraction"] = response
                failed = {sid: f.get("error", "extract 실패") for f in response.get("failures", []) for sid in f.get("block_ids", [])}
                recovered = {}
                for item in response.get("candidates", []):
                    sid = item.get("block_id")
                    if sid not in by_id:
                        continue
                    if item.get("source_field") != "html":
                        failed[sid] = "회수 근거가 source_html에 없음: 주변 문맥을 현재 위치의 광고로 할당하지 않음"
                    elif not valid_binding(item, input_for(by_id[sid]), sid):
                        failed[sid] = "회수 근거의 원문 결합 실패"
                    elif not matching_recovery_text(item["evidence"], by_id[sid]):
                        failed[sid] = "선택 근거와 선별 문구가 다름: 다른 문구의 Laya 캐시를 재사용하지 않고 재검토"
                    else:
                        # 동일 문구·소유 단위만 회수; model selector/technique는 사용하지 않는다.
                        recovered.setdefault(sid, item)
                cleaned = {}
                for call in response.get("calls", []):
                    for bc in call.get("block_calls", []):
                        sid = bc.get("input", {}).get("candidate_id", bc.get("block_id"))
                        parsed = bc.get("parsed", {})
                        if sid in by_id and bc.get("status") == "ok" and parsed.get("verdict") == "clean":
                            if valid_binding(parsed, input_for(by_id[sid]), sid):
                                cleaned[sid] = parsed
                for sid, row in by_id.items():
                    if sid in failed:
                        unresolved(row, failed[sid])
                    elif sid in recovered:
                        data = {"frame_url": row["url"], "hidden": [row] if row.get("signals") else [],
                                "texts": [] if row.get("signals") else [row], "head": [], "links": []}
                        new = candidates_from([(row["url"], [(row["prefix"], data)])],
                                              detect_posted=detect_posted, recover_posted=True)
                        if not new and not detect_posted:
                            stats["source_results"].append({"source_id": sid, "processing_status": "excluded",
                                                            "reason": "게시형 탐지 제외 설정", "extraction": recovered[sid]})
                            continue
                        if not new:
                            unresolved(row, "광고 근거 회수됨; 원문에서 기법을 확정할 수 없음")
                            stats["source_results"].append({"source_id": sid, "extraction": recovered[sid]})
                            continue
                        judge(new, model_path, False)
                        for c in new:
                            c["extraction"] = recovered[sid]
                            if c["verdict"] == "borderline" or c.get("recovered_without_rule_hit"):
                                review(c, "회수된 새 문구 AI 재판정 대기")
                                route(c)
                            else:
                                c.update(processing_status="completed", reason="회수 후 기존 규칙 확정")
                        cands.extend(new)
                    elif sid in cleaned:
                        stats["source_results"].append({"source_id": sid, "verdict": "clean", "decided_by": "gemma", "binding": cleaned[sid]})
                    else:
                        unresolved(row, "extract 출력 누락 또는 원문 결합 실패")
            except Exception as exc:
                check()
                for row in extract_rows:
                    unresolved(row, f"{type(exc).__name__}: {exc}", "timeout" if isinstance(exc, TimeoutError) else "error")

        for index, c in enumerate(pending):
            if not check():
                review(c, "공유 점검 시간 예산 초과", "timeout")
                continue
            if c.get("attr", "").startswith("::"):
                review(c, "CSS 가상 문구는 원본 HTML 텍스트에 결합할 수 없음")
                continue
            if not c.get("source_html"):
                review(c, "원본 HTML 없음: 재합성하지 않음")
                continue
            item = {"candidate_id": f"{c.get('source_id', 'source')}:{index}", **input_for(c)}
            try:
                gemma = get_client()
                stats["gemma_verify"] += 1
                response = gemma.verify(item)
                if not check():
                    raise TimeoutError("Gemma 검증 중 공유 시간 예산 초과")
                c["gemma"] = response
                rows = response.get("results", [])
                row = rows[0] if len(rows) == 1 else {}
                if (response.get("status") != "ok" or row.get("status") != "ok" or row.get("needs_review")
                        or row.get("candidate_id") != item["candidate_id"]
                        or row.get("verdict") not in ("ad", "clean")
                        or not valid_binding(row, item, item["candidate_id"])):
                    review(c, "Gemma 보류/누락/원문 결합 실패")
                elif row["verdict"] == "ad" and row.get("source_field") != "html":
                    review(c, "광고 근거가 source_html에 없음: 주변 문맥만으로 현재 위치를 확정할 수 없음")
                else:
                    c.update(verdict=row["verdict"], decided_by="gemma", processing_status="completed", reason="Gemma v15 원문 결합 검증 완료")
            except Exception as exc:
                check()
                review(c, f"{type(exc).__name__}: {exc}", "timeout" if isinstance(exc, TimeoutError) else "error")
    check()
    stats["failures"].extend({"source_id": c.get("source_id"), "stage": "verify", "reason": c["reason"]}
                             for c in pending if c["verdict"] == "uncertain")
    return stats
