"""AI 집계와 원문을 포함하지 않는 실행별 감사 기록."""
import copy
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path


def context_weight(row):
    weights = [signal[1] for signal in (row.get('context') or [])
               if isinstance(signal, (tuple, list)) and len(signal) == 2
               and signal[0] in ('page-context', 'body-context')
               and type(signal[1]) in (int, float) and math.isfinite(signal[1]) and signal[1] < 0]
    return min(weights) if weights else None


def context_conflict(row, p, threshold):
    return (context_weight(row) is not None and math.isfinite(p)
            and .5 <= p <= 1 and max(p, 1-p) >= threshold)


class AIProgress:
    def __init__(self, observer=None, *, allow_context_conflict=False):
        self.observer = observer
        self.allow_context_conflict = allow_context_conflict
        self.snapshot = {
            "stage": "laya", "laya": dict.fromkeys(
                ("total", "attempted", "completed", "failed", "preflight_rejected", "in_flight", "remaining"), 0),
            "gemma": dict.fromkeys(("eligible_verify", "eligible_extract", "settled", "remaining",
                                    "in_flight", "requests_started", "responses_completed", "requests_failed",
                                    "output_failed", "preflight_failed", "reused"), 0),
            "audit_degraded": False, "audit_errors": 0}
        self.owners = {}
        self.requests = {}

    def error(self):
        self.snapshot["audit_degraded"] = True
        self.snapshot["audit_errors"] += 1

    def emit(self, event):
        if self.observer is not None:
            try:
                self.observer(copy.deepcopy(event))
            except Exception:
                self.error()

    def progress(self, stage=None):
        if stage is not None:
            self.snapshot["stage"] = stage
        laya, gemma = self.snapshot["laya"], self.snapshot["gemma"]
        laya["remaining"] = laya["total"] - laya["completed"] - laya["failed"]
        gemma["remaining"] = gemma["eligible_verify"] + gemma["eligible_extract"] - gemma["settled"]
        self.emit({"event": "progress", "snapshot": self.snapshot})
        return copy.deepcopy(self.snapshot)

    def register(self, owner, row, route, p, threshold, source_sha256, input_sha256):
        # 진행 중 owner의 판정 메타데이터만 보관한다. 종료 이벤트에서 제거한다.
        self.owners[owner] = {"p": p, "confidence": max(p, 1-p), "threshold": threshold,
                              "source_id": row.get("source_id"), "owner_id": owner, "route_path": route,
                              "source_sha256": source_sha256, "input_sha256": input_sha256,
                              "eligibility": 'context_conflict' if route == 'verify_context_conflict' else 'low_confidence',
                              "context_weight": context_weight(row)}

    def event(self, event):
        name = event.get("event")
        if name == "audit_error":
            self.error()
            self.progress()
            return
        owner = event.get("candidate_id")
        meta = self.owners.get(owner)
        invalid = (meta is None or not meta.get("source_id")
                or not (.5 <= meta["threshold"] <= 1 and math.isfinite(meta["p"])
                        and 0 <= meta["p"] <= 1 and (meta["confidence"] < meta["threshold"]
                        or (self.allow_context_conflict and meta.get('route_path') == 'verify_context_conflict'
                            and meta.get('eligibility') == 'context_conflict' and meta['p'] >= .5
                            and type(meta.get('context_weight')) in (int, float)
                            and math.isfinite(meta['context_weight']) and meta['context_weight'] < 0)))
                or any(key in event and event[key] != value for key, value in (meta or {}).items()))
        request_id = event.get("request_id")
        request = self.requests.get(owner)
        if not isinstance(request_id, str) or not request_id:
            invalid = True
        if name == "request_start":
            retry = (request is not None and request[1] == "ended" and request[0] != request_id
                     and event.get("decision_retry") is True and not (meta or {}).get("decision_retry_used"))
            invalid = invalid or (request is not None and not retry) or event.get("request_count") != 1
        elif name == "request_end":
            invalid = invalid or request != (request_id, "started") or event.get("request_count") != 1
        elif name == "item_end":
            invalid = invalid or (request != (request_id, "ended") if request is not None
                                  else event.get("request_count") != 0)
        else:
            invalid = True
        record = {**event, **(meta or {})}
        if invalid:
            self.error()
            self.emit({**record, "event": "audit_error"})
            self.progress()
            return
        gemma = self.snapshot["gemma"]
        if name == "request_start":
            if request is not None:
                meta["decision_retry_used"] = True
            self.requests[owner] = (request_id, "started")
            gemma["requests_started"] += 1
            gemma["in_flight"] += 1
        elif name == "request_end":
            self.requests[owner] = (request_id, "ended")
            gemma["in_flight"] = max(0, gemma["in_flight"] - 1)
            if event.get("completion_received"):
                gemma["responses_completed"] += 1
                if event.get("status") != "ok":
                    gemma["output_failed"] += 1
            else:
                gemma["requests_failed"] += 1
        elif name == "item_end" and meta is not None:
            gemma["settled"] += 1
            gemma["preflight_failed"] += int(bool(event.get("preflight_failed")))
            gemma["reused"] += int(event.get("status") == "reused")
            self.owners.pop(owner, None)
            self.requests.pop(owner, None)
        self.emit(record)
        self.progress("gemma")


class AuditPublisher:
    def __init__(self, out, observer=None):
        self.run_id = uuid.uuid4().hex
        self.audit_path = f"ai-audit/{self.run_id}.jsonl"
        self.path = Path(out) / self.audit_path
        self.observer = observer
        self.errors = 0
        self.snapshot = None

    def enrich(self, snapshot):
        result = copy.deepcopy(snapshot)
        result.update(run_id=self.run_id, audit_path=self.audit_path)
        result["audit_errors"] = snapshot.get("audit_errors", 0) + self.errors
        result["audit_degraded"] = bool(snapshot.get("audit_degraded") or self.errors)
        return result

    def __call__(self, event):
        # 고정 허용 필드만 기록한다. 예외 문구·입력·모델 응답은 기록하지 않는다.
        allowed = ("event", "request_id", "candidate_id", "task", "status", "completion_received",
                   "request_count", "preflight_failed", "p", "confidence", "threshold", "source_id",
                   "owner_id", "route_path", "source_sha256", "input_sha256", "cache_hit", "cache_key", "reuse_kind",
                   "eligibility", "context_weight", "decision_retry")
        record = {key: copy.deepcopy(event[key]) for key in allowed if key in event}
        record.update(run_id=self.run_id, time=datetime.now(timezone.utc).isoformat())
        if event.get("event") == "progress":
            record["snapshot"] = self.enrich(event["snapshot"])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
        except Exception:
            self.errors += 1
        if "snapshot" in record:
            record["snapshot"] = self.enrich(event["snapshot"])
        if self.observer is not None:
            try:
                self.observer(copy.deepcopy(record))
            except Exception:
                self.errors += 1
        if "snapshot" in record:
            self.snapshot = copy.deepcopy(event["snapshot"])
