"""규칙 경계 검증과 수집된 DOM 잔여 회수. 실모델은 필요한 단계에서만 연다."""
import hashlib
import html
import json
import math
import re
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


_ROOT_ATTR = re.compile(r'''\s+([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?''')


class _OwnerText(HTMLParser):
    """하위 요소의 소유 관계를 추측하지 않고 단일 요소의 본문만 읽는다."""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.tag = None
        self.closed = False
        self.parts = []
        self.opening = ""
        self.body_start = self.body_end = 0

    def _char_offset(self):
        line, column = self.getpos()
        return sum(len(part) + 1 for part in self.source.split("\n")[:line - 1]) + column

    def handle_starttag(self, tag, attrs):
        if tag == "br" and not attrs and self.tag is not None and not self.closed:
            return
        if self.tag is not None or tag in ("script", "style", "textarea", "template", "noscript"):
            raise ValueError("본문 소유가 불명확한 HTML")
        self.tag = tag
        self.text_attrs = [name for name, value in attrs
                           if name in ("alt", "title", "aria-label") and value and value.strip()]
        self.has_text_attrs = bool(self.text_attrs)
        self.opening = self.get_starttag_text()
        self.body_start = self._char_offset() + len(self.opening)
        if tag == "meta":
            self.closed = True

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not attrs and self.tag is not None and not self.closed:
            return
        self.handle_starttag(tag, attrs)
        if tag != "meta":
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.tag != tag or self.closed:
            raise ValueError("단일 요소가 아닌 HTML")
        self.closed = True
        self.body_end = self._char_offset()

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

    def attribute_span(self, name):
        # outerHTML의 인용된 속성값만 지원하며, 다른 속성값 내부를 재검색하지 않는다.
        position, matches = len(self.tag) + 1, []
        while match := _ROOT_ATTR.match(self.opening, position):
            if match[1].lower() == name:
                group = 2 if match[2] is not None else 3
                if match[group] is None:
                    return None
                base = self.body_start - len(self.opening)
                matches.append((base + match.start(group), base + match.end(group), match[group]))
            position = match.end()
        if self.opening[position:].strip() not in (">", "/>") or len(matches) != 1:
            return None
        return matches[0]


def matching_recovery_text(binding, row):
    """검증된 binding의 실제 범위를 현재 본문/속성 소유 단위에 결합한다."""
    evidence = binding["evidence"]
    expected = normalize(row["evidence_text"] if "evidence_text" in row else row["text"])
    bare_match = normalize(html.unescape(evidence)) == expected
    source = row.get("source_html", "")
    if binding.get("source_field") != "html":
        return False
    parser = _OwnerText(source)
    try:
        parser.feed(source)
        parser.close()
    except ValueError:
        return False
    if parser.tag != row.get("tag", "").lower():
        return False
    attr = row.get("attr")
    if parser.tag == "meta":
        span = parser.attribute_span("content")
        return (not attr and not parser.has_text_attrs and source == parser.opening and evidence == source
                and binding["start_char"] == 0 and binding["end_char"] == len(source)
                and span is not None and bool(normalize(html.unescape(span[2])))
                and normalize(html.unescape(span[2])) == expected)
    if attr:
        if attr not in ("alt", "title", "aria-label") or not (parser.closed or source == parser.opening):
            return False
        span = parser.attribute_span(attr)
        if span is None:
            return False
        value = normalize(html.unescape(span[2]))
        if (parser.tag == "img" and source == parser.opening and evidence == source
                and binding["start_char"] == 0 and binding["end_char"] == len(source)):
            # img 루트 단독 원문 전체는 그 요소의 유일한 비어 있지 않은 텍스트 속성에만 결합한다.
            return parser.text_attrs == [attr] and bool(value) and value == expected
        return (bare_match and span[0] <= binding["start_char"] < binding["end_char"] <= span[1]
                and value == expected)
    if not parser.closed:
        return False
    if evidence == source:
        return not parser.has_text_attrs and normalize("".join(parser.parts)) == expected
    # 같은 문구라도 선택 offset이 루트 직접 본문에 속해야 한다.
    return (bare_match
            and parser.body_start <= binding["start_char"] < binding["end_char"] <= parser.body_end)


def analyze(cands, pages, model_path, use_ml, deadline=None, cancel=None, laya_confidence_threshold=.8, cancel_error=None, detect_posted=True, telemetry=None):
    from .ai_telemetry import AIProgress
    from .main import ScanCancelled, candidates_from, judge, source_fields
    ScanCancelled = cancel_error or ScanCancelled
    if not .5 <= laya_confidence_threshold <= 1:
        raise ValueError("laya_confidence_threshold는 0.5~1 범위여야 합니다")
    stats = {"mode": "policy" if use_ml else "diagnostic", "laya_confidence_threshold": laya_confidence_threshold,
             "policy": "laya_low_confidence_only",
             "laya_inputs": 0, "gemma_verify": 0, "gemma_extract": 0, "residual_screened_clean": 0,
             "gemma_requests": dict.fromkeys(("attempted", "succeeded", "transport_failed", "output_failed", "preflight_failed", "reused"), 0),
             "source_rows": 0, "unique_count": 0, "cache_reuse": 0,
             "laya_sec": 0.0, "gemma_verify_sec": 0.0, "gemma_extract_sec": 0.0,
             "unresolved_sources": [], "failures": [], "source_results": [],
             "scope": "수집된 DOM 텍스트 소유 단위; 전체 HTTP 원문 및 이미지 검사는 포함하지 않음"}

    meter = AIProgress(telemetry)
    stats["telemetry"] = meter.snapshot

    def finish():
        laya, gemma = meter.snapshot["laya"], meter.snapshot["gemma"]
        complete = (use_ml and check() and laya["completed"] + laya["failed"] == laya["total"]
                    and gemma["settled"] == gemma["eligible_verify"] + gemma["eligible_extract"])
        stats["telemetry"] = meter.progress(
            "complete" if complete else "incomplete" if use_ml else "diagnostic")
        return stats

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

    # 이 호출의 model/QUESTION은 고정. Laya 입력만 공유하고 Gemma 원문 결합은 owner별 유지.
    def key(row):
        return normalize(row.get("evidence_text", row.get("text", "")))

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

    predictions = {}
    rows_by_key = {}
    for row in [c for c in border if not about(c)] + residual:
        rows_by_key.setdefault(key(row), []).append(row)
    stats["source_rows"] = sum(len(owners) for owners in rows_by_key.values())
    stats["unique_count"] = len(rows_by_key)
    stats["cache_reuse"] = stats["source_rows"] - stats["unique_count"]
    laya_counts = meter.snapshot["laya"]
    laya_counts["total"] = len(rows_by_key)
    meter.progress("laya")
    if not use_ml:
        for c in border:
            review(c, "명시적 진단 모드: AI 판정 생략")
        for row in residual:
            unresolved(row, "명시적 진단 모드: 잔여 선별 생략")
        return finish()

    laya_started = time.perf_counter()
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
                    laya_counts["attempted"] += len(chunk)
                    laya_counts["in_flight"] += len(chunk)
                    settled_before = laya_counts["completed"] + laya_counts["failed"]
                    meter.progress()
                    try:
                        outputs = runner.predict_batch(chunk, QUESTION, batch_size=16)
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
                                laya_counts["completed"] += 1
                            except (IndexError, KeyError, TypeError, ValueError):
                                laya_counts["failed"] += 1
                                stats["failures"].extend({"source_id": owner.get("source_id"), "stage": "laya", "reason": "출력 누락/잘림/오류"}
                                                         for owner in rows_by_key[k])
                    except LayaCleanupError:
                        raise
                    except Exception as exc:
                        check()
                        settled = laya_counts["completed"] + laya_counts["failed"] - settled_before
                        laya_counts["failed"] += len(chunk) - settled
                        stats["failures"].extend({"source_id": owner.get("source_id"), "stage": "laya", "reason": str(exc)}
                                                 for k in chunk for owner in rows_by_key[k])
                    finally:
                        laya_counts["in_flight"] -= len(chunk)
                        meter.progress()
    except LayaCleanupError as exc:
        # __enter__ 실패 때도 PID/정리 단계가 포함된 전용 타입으로 Gemma 시작을 차단한다.
        stats["failures"].extend({"source_id": r.get("source_id"), "stage": "laya_cleanup", "reason": str(exc)}
                                 for owners in rows_by_key.values() for r in owners)
        for c in border:
            review(c, str(exc), "error")
        for row in residual:
            unresolved(row, str(exc), "error")
        meter.progress("incomplete")
        return stats
    except Exception as exc:
        check()
        stats["failures"].extend({"source_id": r.get("source_id"), "stage": "laya", "reason": str(exc)} for owners in rows_by_key.values() for r in owners)
    finally:
        stats["laya_sec"] = round(time.perf_counter() - laya_started, 3)
    check()

    pending = []
    def route(c):
        if not c.get("source_html"):
            review(c, "원본 HTML 없음: 재합성하지 않음")
            stats["failures"].append({"source_id": c.get("source_id"), "stage": "source", "reason": c["reason"]})
            return
        p = None if about(c) else predictions.get(key(c))
        if p is None:
            review(c, "문맥 검토 대상 또는 유효 Laya 확률 없음: Gemma 대체 판정 금지")
            return
        c["ml_prob"] = round(p, 4)
        c["laya_confidence"] = max(p, 1 - p)
        if max(p, 1 - p) >= laya_confidence_threshold:
            c.update(verdict="ad" if p >= .5 else "clean", decided_by="laya",
                     processing_status="completed", reason="Laya 정상 출력의 confidence가 운영 임계값 이상")
        else:
            pending.append(c)
            meter.snapshot["gemma"]["eligible_verify"] += 1
            meter.progress("gemma")
    for c in border:
        route(c)

    def recover_row(row, extraction=None):
        data = {"frame_url": row["url"], "hidden": [row] if row.get("signals") else [],
                "texts": [] if row.get("signals") else [row], "head": [], "links": []}
        new = candidates_from([(row["url"], [(row["prefix"], data)])],
                              detect_posted=detect_posted, recover_posted=True)
        if not new and not detect_posted:
            result = {"source_id": row["source_id"], "processing_status": "excluded",
                      "reason": "게시형 탐지 제외 설정"}
            if extraction is not None:
                result["extraction"] = extraction
            stats["source_results"].append(result)
            return
        if not new:
            unresolved(row, "광고 근거 회수됨; 원문에서 기법을 확정할 수 없음")
            if extraction is not None:
                stats["source_results"].append({"source_id": row["source_id"], "extraction": extraction})
            return
        judge(new, model_path, False)
        for c in new:
            if extraction is not None:
                c["extraction"] = extraction
            if (extraction is not None and c["verdict"] != "borderline"
                    and not c.get("recovered_without_rule_hit")):
                c.update(processing_status="completed", reason="회수 후 기존 규칙 확정")
            elif key(c) != key(row):
                review(c, "회수 문구와 선별 문구가 다름: Laya 확률 재사용 및 Gemma 판정 금지")
            elif about(c):
                review(c, "문맥 검토 대상: 유효 Laya 확률 없음")
            else:
                route(c)
        cands.extend(new)

    extract_rows = []
    for row in residual:
        p = predictions.get(key(row))
        if not row.get("source_html"):
            unresolved(row, "원본 HTML 없음: 재합성하지 않음")
        elif p is None:
            unresolved(row, "유효 Laya 확률 없음: Gemma 대체 추출 금지")
        elif max(p, 1 - p) >= laya_confidence_threshold:
            if p < .5:
                stats["residual_screened_clean"] += 1
                stats["source_results"].append({"source_id": row["source_id"], "verdict": "clean", "decided_by": "laya"})
            else:
                stats["source_results"].append({**source_fields(row), "verdict": "ad", "decided_by": "laya",
                                                "ml_prob": round(p, 4), "laya_confidence": max(p, 1 - p)})
                recover_row(row)
        else:
            extract_rows.append(row)
            meter.snapshot["gemma"]["eligible_extract"] += 1
            meter.progress("gemma")

    with ExitStack() as stack:
        client = None
        client_error = None
        inner_proofs = {}

        def count_requests(response):
            # extract wrapper의 합계와 block leaf를 중복 집계하지 않는다.
            if "block_calls" in response:
                for call in response["block_calls"]:
                    count_requests(call)
            elif "calls" in response:
                for call in response["calls"]:
                    count_requests(call)
                stats["gemma_requests"]["preflight_failed"] += sum(
                    f.get("preflight_failed", 0) for f in response.get("failures", []))
            elif "results" in response:
                for row in response["results"]:
                    count_requests(row.get("call", {}))
            else:
                counts = stats["gemma_requests"]
                attempted = response.get("request_count", 0)
                counts["attempted"] += attempted
                counts["preflight_failed"] += response.get("preflight_failed", 0)
                if attempted:
                    kind = ("succeeded" if response.get("status") == "ok" else
                            "output_failed" if response.get("completion_received") or "raw_response" in response else
                            "transport_failed")
                    counts[kind] += attempted

        def reusable_proof(c):
            extraction = c.get("extraction", {})
            owner = extraction.get("block_id")
            call = inner_proofs.get(owner, {})
            parsed = call.get("parsed", {})
            item = {"candidate_id": owner, **input_for(c)}
            if (call.get("status") != "ok" or call.get("validation", {}).get("errors")
                    or call.get("input") != item or parsed.get("verdict") != "ad"
                    or not valid_binding(parsed, item, owner)
                    or any(parsed.get(k) != extraction.get(k) for k in
                           ("evidence", "evidence_id", "owner_id", "source_field", "start_char", "end_char", "source_sha256"))
                    or not matching_recovery_text(parsed, c)):
                return None
            return {"task": "verify", "version": "v15", "status": "ok", "results": [
                {**parsed, "candidate_id": owner, "status": "ok", "needs_review": False, "call": call}]}

        def get_client():
            nonlocal client, client_error
            if not check():
                raise TimeoutError("공유 점검 시간 예산 초과")
            if client_error:
                raise RuntimeError(client_error)
            if client is None:
                try:
                    from local_llm.client import GemmaClient
                    # Budget의 None은 30분 기본값이므로 무제한을 명시한다. 보고서에는 저장하지 않는다.
                    client = stack.enter_context(GemmaClient(deadline=math.inf if deadline is None else deadline, cancel=cancel, on_event=meter.event))
                except Exception as exc:
                    client_error = str(exc)
                    raise
            return client

        def register(owner, row, route):
            p = predictions[key(row)]
            meter.register(owner, row, route, p, laya_confidence_threshold,
                           digest(input_for(row)), hashlib.sha256(key(row).encode()).hexdigest())

        def item_end(owner, status, preflight_failed=0):
            if owner not in meter.owners:
                return
            import uuid
            meter.event({"event": "item_end", "request_id": uuid.uuid4().hex,
                         "candidate_id": owner, "task": "verify", "status": status,
                         "request_count": 0, "completion_received": False,
                         "preflight_failed": preflight_failed})

        def verify_pending(start, end):
            verify_started = time.perf_counter()
            for index in range(start, end):
                c = pending[index]
                if not check():
                    review(c, "공유 점검 시간 예산 초과", "timeout")
                    continue
                owner = f"{c.get('source_id', 'source')}:{index}"
                register(owner, c, "verify")
                if c.get("attr", "").startswith("::"):
                    item_end(owner, "preflight_failed", 1)
                    review(c, "CSS 가상 문구는 원본 HTML 텍스트에 결합할 수 없음")
                    continue
                if not c.get("source_html"):
                    item_end(owner, "preflight_failed", 1)
                    review(c, "원본 HTML 없음: 재합성하지 않음")
                    continue
                item = {"candidate_id": f"{c.get('source_id', 'source')}:{index}", **input_for(c)}
                try:
                    response = reusable_proof(c)
                    if response is not None:
                        item["candidate_id"] = c["extraction"]["block_id"]
                        meter.owners[item["candidate_id"]] = meter.owners.pop(owner)
                        meter.owners[item["candidate_id"]]["owner_id"] = item["candidate_id"]
                        item_end(item["candidate_id"], "reused")
                        stats["gemma_requests"]["reused"] += 1
                    else:
                        gemma = get_client()
                        stats["gemma_verify"] += 1
                        response = gemma.verify(item)
                        count_requests(response)
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
                    elif row["verdict"] == "ad" and not matching_recovery_text(row, c):
                        review(c, "선택 광고 근거가 현재 문구의 직접 본문에 결합되지 않음: 소유 관계 재검토")
                    elif row["verdict"] == "clean" and row.get("source_field") != "html":
                        review(c, "정상 근거가 대상 source_html에 없음: 주변 문맥만으로 정상 확정 불가")
                    elif row["verdict"] == "clean" and not matching_recovery_text(row, c):
                        review(c, "선택 정상 근거가 현재 문구의 직접 본문에 결합되지 않음: 소유 관계 재검토")
                    else:
                        c.update(verdict=row["verdict"], decided_by="gemma", processing_status="completed", reason="Gemma v15 원문 결합 검증 완료")
                except Exception as exc:
                    check()
                    item_end(item["candidate_id"], "preflight_failed", 1)
                    review(c, f"{type(exc).__name__}: {exc}", "timeout" if isinstance(exc, TimeoutError) else "error")
            stats["gemma_verify_sec"] = round(stats["gemma_verify_sec"] + time.perf_counter() - verify_started, 3)

        initial_pending = len(pending)
        verify_pending(0, initial_pending)

        if extract_rows:
            extract_started = time.perf_counter()
            blocks = [{"block_id": r["source_id"], **input_for(r)} for r in extract_rows]
            by_id = {r["source_id"]: r for r in extract_rows}
            for row in extract_rows:
                register(row["source_id"], row, "extract")
            try:
                gemma = get_client()
                stats["gemma_extract"] += len(blocks)
                response = gemma.extract({"blocks": blocks})
                count_requests(response)
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
                    elif not matching_recovery_text(item, by_id[sid]):
                        failed[sid] = "선택 근거와 선별 문구가 다름: 다른 문구의 Laya 캐시를 재사용하지 않고 재검토"
                    else:
                        # 동일 문구·소유 단위만 회수; model selector/technique는 사용하지 않는다.
                        recovered.setdefault(sid, item)
                cleaned = {}
                for call in response.get("calls", []):
                    for bc in call.get("block_calls", []):
                        sid = bc.get("input", {}).get("candidate_id", bc.get("block_id"))
                        parsed = bc.get("parsed", {})
                        if sid in by_id:
                            inner_proofs[sid] = bc
                        if sid in by_id and bc.get("status") == "ok" and parsed.get("verdict") == "clean":
                            if (bc.get("input") == {"candidate_id": sid, **input_for(by_id[sid])}
                                    and parsed.get("source_field") == "html"
                                    and not bc.get("validation", {}).get("errors")
                                    and valid_binding(parsed, input_for(by_id[sid]), sid)
                                    and matching_recovery_text(parsed, by_id[sid])):
                                cleaned[sid] = parsed
                for sid, row in by_id.items():
                    if sid in failed:
                        unresolved(row, failed[sid])
                    elif sid in recovered:
                        recover_row(row, recovered[sid])
                    elif sid in cleaned:
                        stats["source_results"].append({"source_id": sid, "verdict": "clean", "decided_by": "gemma", "binding": cleaned[sid]})
                    else:
                        unresolved(row, "extract 출력 누락 또는 원문 결합 실패")
            except Exception as exc:
                check()
                for row in extract_rows:
                    item_end(row["source_id"], "preflight_failed", 1)
                    unresolved(row, f"{type(exc).__name__}: {exc}", "timeout" if isinstance(exc, TimeoutError) else "error")
            finally:
                stats["gemma_extract_sec"] = round(time.perf_counter() - extract_started, 3)

        verify_pending(initial_pending, len(pending))
    check()
    stats["failures"].extend({"source_id": c.get("source_id"), "stage": "verify", "reason": c["reason"]}
                             for c in pending if c["verdict"] == "uncertain")
    return finish()
