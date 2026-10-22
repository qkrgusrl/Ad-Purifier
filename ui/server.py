"""광고청정기 화면: 로컬 웹 서버 + 브라우저 UI.

실행하면 127.0.0.1에 서버를 띄우고 기본 브라우저로 화면을 연다.
탐지 결과는 요강대로 구동 폴더(app_dir)에 result.json/report.json으로 쓰고,
점검 이력은 history/<실행시각>/ 에 report.json, result.json, review.json(검토 상태·메모)으로 쌓는다.
이력 삭제는 history/ 아래 실행 ID 형식 폴더만 지운다 (구동 폴더의 제출용 result.json은 그대로).

사용: python -m ui.server [--port 8780] [--no-browser]
"""
import argparse
import csv
import copy
import hashlib
import io
import importlib.util
import json
import re
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app_paths import bootstrap, resource_root, gemma_model, llama_server

from detector.main import DEFAULT_MODEL, DOWNLOAD_ERROR, JUDGE_RESERVE, KST, LLM_RESERVE, TIME_BUDGET, ScanCancelled, app_dir, run

CRAWL_LIMIT = TIME_BUDGET - LLM_RESERVE - JUDGE_RESERVE  # 화면 진행률에 쓰는 크롤링 최대 시간(초)
options = {"complete_validation": False}

STATIC = resource_root() / "ui" / "static"
HISTORY = app_dir() / "history"
RUN_ID = re.compile(r"\d{8}-\d{6}")
CAND_KEY = re.compile(r"[0-9a-f]{12}")
REVIEW_STATES = {"new", "confirmed", "false_positive", "resolved"}

state_lock = threading.Lock()
state = {"running": False, "phase": "idle", "entry_url": "", "pages_done": 0, "pages_found": 0, "current_url": "",
         "started": None, "run_id": None, "error": None, "cancelling": False, "ai_progress": None}
cancel_event = threading.Event()  # 탐지 취소 신호 (진행 중인 점검 하나에만 쓰고, 새 점검을 시작할 때 비운다)


def join_scan_worker(httpd):
    """취소 후 scan context가 자식을 정리하고 반환할 때까지 기다린다."""
    cancel_event.set()
    worker = getattr(httpd, "scan_worker", None)
    if worker is not None:
        worker.join()


def shutdown_server(httpd):
    join_scan_worker(httpd)
    httpd.shutdown()


def model_readiness():
    """파일 준비와 실제 실행 검증은 다르다. 상태 API에서 모델을 실행하지 않는다."""
    files = {
        "laya_weights": DEFAULT_MODEL / "model.safetensors",
        "laya_config": DEFAULT_MODEL / "rl_agent_config.json",
        "laya_questions": DEFAULT_MODEL / "questions.json",
        "encoder": DEFAULT_MODEL / "encoder" / "config.json",
        "tokenizer": DEFAULT_MODEL / "tokenizer" / "tokenizer.json",
        "tokenizer_config": DEFAULT_MODEL / "tokenizer" / "tokenizer_config.json",
        "gemma_weights": gemma_model(), "llama_server": llama_server(),
    }
    if sys.platform == "win32":
        # b11429 공식 CPU 배포물의 기본 backend와 서버 실행 의존 파일.
        for name in ("llama.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu-x64.dll", "ggml-cpu-sse42.dll",
                     "llama-common.dll", "mtmd.dll", "libomp.dll", "llama-server-impl.dll",
                     "MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
            files[name] = llama_server().parent / name
    available = {name: path.is_file() and path.stat().st_size > 0 for name, path in files.items()}
    dependencies = {}
    for name in ("laya", "torch", "transformers", "tokenizers", "safetensors", "playwright"):
        try:
            dependencies[name] = importlib.util.find_spec(name) is not None
        except (ValueError, ImportError):
            dependencies[name] = False
    return {"files": available, "dependencies": dependencies,
            "files_present": all(available.values()) and all(dependencies.values()),
            "execution_status": "not_checked", "note": "파일 존재 확인만 수행; 모델 로딩·추론 성공을 뜻하지 않음"}


def cand_key(c):
    if c.get("source_id"):
        # 새 문구에 이전 위치 단위의 검토 상태를 일괄 적용하지 않는다.
        identity = [c["url"], c["location"], c["technique"], c["source_id"],
                    c.get("attr", ""), c["evidence_text"]]
        payload = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    else:
        payload = f"{c['url']}|{c['location']}|{c['technique']}"
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def scan_job(entry_url, use_ml):
    with state_lock:
        started = state["started"]

    def observer(event):
        if event.get("event") != "progress":
            return
        with state_lock:
            if state["started"] != started:
                return
            state["ai_progress"] = copy.deepcopy(event["snapshot"])

    def progress(done, found, url):
        with state_lock:
            state.update(pages_done=done, pages_found=found, current_url=url)

    def phase(name):
        with state_lock:
            if not state["cancelling"]:
                state["phase"] = name

    run_id = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    try:
        report = run(entry_url, app_dir(), use_ml=use_ml, progress=progress, phase=phase,
            cancel=cancel_event, complete_validation=options["complete_validation"], telemetry=observer)
        dest = HISTORY / run_id
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("result.json", "report.json"):
            shutil.copy2(app_dir() / name, dest / name)
        # 추가 탐지(게시형) 결과는 있을 때만 생긴다 — 다음 점검이 구동 폴더 파일을 덮어써도 이력에서 받을 수 있게 보관
        if (app_dir() / "result_extra.json").is_file():
            shutil.copy2(app_dir() / "result_extra.json", dest / "result_extra.json")
        with state_lock:
            state.update(phase="review" if report.get("needs_review") else "done", run_id=run_id)
    except ScanCancelled:  # 결과 파일·이력은 만들지 않는다 (지난 점검의 result.json 그대로)
        with state_lock:
            state.update(phase="cancelled")
    except Exception as e:
        traceback.print_exc()
        with state_lock:
            state.update(phase="error", error=f"{type(e).__name__}: {e}")
    finally:
        with state_lock:
            state.update(running=False, cancelling=False)


def restore_review_state(rep):
    """이전 보고서의 미판정 상태를 조회 메모리에서만 복원한다."""
    for c in rep.get("candidates", []):
        if c.get("verdict") not in ("ad", "clean") or c.get("processing_status") in ("review", "error", "timeout", "cancelled"):
            c["verdict"] = "uncertain"
            if c.get("processing_status") in (None, "completed"):
                c["processing_status"] = "review"
            c.setdefault("reason", "이전 보고서의 미판정 항목: 재검토 필요")
    from detector.review import annotate_reviews
    annotate_reviews(rep)
    pending = sum(c.get("verdict") == "uncertain" for c in rep.get("candidates", []))
    pending += len(rep.get("ai", {}).get("unresolved_sources", []))
    rep["unresolved_count"] = max(rep.get("unresolved_count", 0), pending)
    # 이전 점검에서 접속 실패로 기록된 파일 다운로드 주소(down.php 등)는 다운로드 링크로 다시 분류한다 (원본 파일은 그대로)
    entry = rep.get("meta", {}).get("entry_url")
    downloads = [f for f in rep.get("failed_pages", []) if DOWNLOAD_ERROR in str(f.get("reason", "")) and f.get("url") != entry]
    if downloads:
        rep["failed_pages"] = [f for f in rep["failed_pages"] if f not in downloads]
        rep["download_links"] = rep.get("download_links", []) + [{"url": f["url"]} for f in downloads]
        rep.pop("coverage", None)  # 저장된 접속 실패 수를 아래에서 다시 계산
        rep["crawl_incomplete"] = rep["needs_review"] = False  # 남은 미확인 범위·보류로 아래에서 다시 판정
    failures = rep.get("failed_pages", [])
    frames = rep.get("failed_frames", [])
    queued = rep.get("left_in_queue", 0)
    incomplete = bool(rep.get("crawl_incomplete") or rep.get("entry_failed") or failures or frames
                      or queued or rep.get("stopped_by_time") or rep.get("http_errors"))
    rep["crawl_incomplete"] = incomplete
    rep["needs_review"] = bool(rep.get("needs_review") or rep["unresolved_count"] or incomplete
                               or rep.get("ai", {}).get("mode") == "diagnostic")
    if "coverage" not in rep:
        rep["coverage"] = {"visited_pages": rep.get("pages", 0), "pending_pages": queued,
                           "failed_pages": len(failures), "failed_frames": len(frames),
                           "stopped_by_time": rep.get("stopped_by_time", False),
                           "discovered_pages": rep.get("pages", 0) + queued + len(failures)}
    return rep


SUMMARY_VERSION = 1  # 요약 항목·복원 규칙이 바뀌면 올려서 기존 summary.json을 다시 만든다


def run_summary(report_path):
    """목록용 요약. report.json은 정상 문구 문맥까지 담아 수백 MB가 되므로 한 번만 읽고 summary.json에 둔다."""
    st = report_path.stat()
    stamp = [SUMMARY_VERSION, st.st_mtime_ns, st.st_size]
    cache = report_path.parent / "summary.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("stamp") == stamp:
            return cached
    except (OSError, json.JSONDecodeError):
        pass
    rep = restore_review_state(json.loads(report_path.read_text(encoding="utf-8")))
    summary = {"stamp": stamp, "entry_url": rep["meta"]["entry_url"], "started_at": rep["meta"]["started_at"],
               "elapsed_sec": rep["meta"]["elapsed_sec"], "pages": rep["pages"],
               # 검토 상태(review.json)는 수시로 바뀌므로 광고 판정 문구만 남겨 두고 목록 조회 때마다 대조한다
               "ads": [{"key": cand_key(c), "url": c["url"], "also_on": [x["url"] for x in c.get("also_on", [])],
                        "technique": c["technique"], "evidence_text": c["evidence_text"]}
                       for c in rep["candidates"] if c["verdict"] == "ad"],
               "entry_failed": rep.get("entry_failed", False), "needs_review": rep.get("needs_review", False),
               "unresolved_count": rep.get("unresolved_count", 0), "coverage": rep.get("coverage", {})}
    try:
        cache.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return summary


def list_runs():
    runs = []
    for d in sorted(HISTORY.glob("*/report.json"), reverse=True):
        try:
            s = run_summary(d)
        except (OSError, json.JSONDecodeError):
            continue
        review_path = d.parent / "review.json"
        try:
            review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            review = {}
        ads = s["ads"]
        # 처리 필요 = 미검토 + 불법광고 확인 (화면의 광고 청정도 계산용 — 오염 페이지 비율·오염 페이지 수·문구 수·숨김형 건수)
        pending_ads = [c for c in ads if review.get(c["key"], {}).get("status", "new") in ("new", "confirmed")]
        runs.append({"id": d.parent.name, "entry_url": s["entry_url"],
                     "started_at": s["started_at"], "elapsed_sec": s["elapsed_sec"],
                     "pages": s["pages"], "findings": len(ads), "pending": len(pending_ads),
                     # 여러 페이지 반복으로 대표에 합친 페이지(also_on)도 오염 페이지로 센다
                     "dirty_pages": len({u for c in pending_ads for u in [c["url"]] + c["also_on"]}),
                     "pending_hidden": sum(c["technique"] != "POSTED" for c in pending_ads),
                     "pending_unique": len({" ".join(c["evidence_text"].split()) for c in pending_ads}),
                     "entry_failed": s["entry_failed"], "needs_review": s["needs_review"],
                     "unresolved_count": s["unresolved_count"], "coverage": s["coverage"]})
    return runs


def run_dir(run_id):
    # 경로 조작 방지: 실행 ID 형식(YYYYmmdd-HHMMSS)만 허용
    if not RUN_ID.fullmatch(run_id) or not (HISTORY / run_id / "report.json").is_file():
        return None
    return HISTORY / run_id


def load_run(run_id):
    d = run_dir(run_id)
    if not d:
        return None
    rep = restore_review_state(json.loads((d / "report.json").read_text(encoding="utf-8")))
    review_path = d / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
    for c in rep["candidates"]:
        c["key"] = cand_key(c)
        r = review.get(c["key"], {})
        c["review"] = r.get("status", "new")
        c["memo"] = r.get("memo", "")
    rep["id"] = run_id
    return rep


def ui_report(rep):
    """UI 조회 전용 projection. 원문을 복제하거나 저장 객체를 변경하지 않는다."""
    heavy = {"source_html", "source_context", "source_near_text", "context", "raw",
             "gemma", "extraction", "visit_log", "external_skipped", "duplicates"}
    value = {key: item for key, item in rep.items() if key not in heavy}
    value["candidates"] = [
        {key: item for key, item in candidate.items() if key not in heavy}
        for candidate in rep.get("candidates", [])
    ]
    warning_fields = {"source_id", "reason", "processing_status", "url", "location",
                      "status", "error", "message", "stage", "technique", "selector",
                      "frame_url", "attr"}

    def warnings(items):
        return [{key: item for key, item in row.items()
                 if key in warning_fields and not isinstance(item, (dict, list))}
                for row in items]

    for key in ("failed_pages", "failed_frames", "http_errors"):
        if key in rep:
            value[key] = warnings(rep[key])
    ai = rep.get("ai")
    if isinstance(ai, dict):
        value["ai"] = {key: item for key, item in ai.items()
                       if key not in heavy and not isinstance(item, (dict, list))}
        if "unresolved_sources" in ai:
            value["ai"]["unresolved_sources"] = warnings(ai["unresolved_sources"][:5])
            value["ai"]["unresolved_sources_count"] = len(ai["unresolved_sources"])
        if isinstance(ai.get("length_skipped"), list):
            fields = ("source_id", "stage", "url", "selector", "chars", "input_tokens", "token_limit")
            value["ai"]["length_skipped"] = [
                {key: row.get(key) for key in fields if not isinstance(row.get(key), (dict, list))}
                for row in ai["length_skipped"][:20] if isinstance(row, dict)]
    return value


def delete_runs(run_ids):
    """점검 이력 폴더 삭제. 실행 ID 형식이 아닌 이름은 무시해 history 밖이나 다른 파일을 지우지 않는다."""
    deleted = 0
    for run_id in run_ids:
        d = run_dir(run_id)
        if d:
            shutil.rmtree(d, ignore_errors=True)
            deleted += 1
    return deleted


TECH_KO = {"HOMOGLYPH": "닮은꼴 글자", "JAMO": "자모 분해", "TRANSPARENT": "투명 텍스트", "OFFSCREEN": "화면 밖 은닉",
           "POSTED": "게시형 광고"}
REVIEW_KO = {"new": "미검토", "confirmed": "불법광고 확인", "false_positive": "오탐", "resolved": "조치 완료"}


def csv_cell(v):
    """엑셀 수식 주입 방지: 웹페이지에서 가져온 문구가 = + - @ 로 시작하면 문자열로 고정."""
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


# CSV 앞쪽 6열은 result.json(요강 붙임4) findings 필드와 같은 이름·순서·값, 뒤쪽은 담당자용 관리 정보
CSV_HEADER = ["id", "url", "is_violation", "location", "evidence_text", "technique",
              "은닉 기법(한글)", "복원 문구", "판정 근거", "점수", "판정 주체", "검토 상태", "메모", "같은 광고가 있는 다른 페이지 수"]


def export_csv(rep, result_doc):
    """검토용 엑셀 CSV. id는 같은 점검의 result.json과 동일하게 맞춘다 (result.json에 없는 항목은 id 공란)."""
    ids = {(f["url"], f["location"], f["technique"]): f["id"] for f in result_doc.get("findings", [])}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    ads = sorted((c for c in rep["candidates"] if c["verdict"] == "ad"),
                 key=lambda c: ids.get((c["url"], c["location"], c["technique"]), "~"))
    for c in ads:
        w.writerow([csv_cell(v) for v in (
            ids.get((c["url"], c["location"], c["technique"]), ""), c["url"], "true", c["location"], c["evidence_text"],
            c["technique"], TECH_KO.get(c["technique"], c["technique"]), c["normalized"], evidence_of(c), c["score"],
            {"laya": "Laya 판정", "gemma": "젬마 검증"}.get(c["decided_by"], "규칙"), REVIEW_KO.get(c["review"], c["review"]), c["memo"], len(c.get("also_on", [])))])
    # 엑셀에서 한글이 깨지지 않도록 BOM 추가 (검토용 파일 — 제출용 result.json은 BOM 없음)
    return "\ufeff" + buf.getvalue()


def evidence_of(c):
    """판정 근거 열: 항목별 점수가 있으면 '출장마사지(+3), 학생(-1.5)', 예전 이력이면 근거 이름만."""
    if "score_detail" in c:
        return ", ".join(f"{n}({w:+g})" for n, w in c["score_detail"])
    return ", ".join(c["hits"])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 폴링 요청으로 콘솔이 넘치지 않게
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        """다른 웹사이트가 이 로컬 서버를 조작하지 못하게 한다.

        - Host 검사: DNS 리바인딩 차단 (127.0.0.1 / localhost 로 접속한 경우만 허용)
        - POST는 application/json만: 다른 출처의 페이지는 사전 확인(preflight) 없이 이 형식을 보낼 수 없다
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost"):
            return False
        if self.command == "POST":
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            with state_lock:
                s = dict(state)
            s["elapsed"] = round(time.time() - s["started"], 1) if s["started"] else 0
            s["model_readiness"] = model_readiness()
            s["model_available"] = s["model_readiness"]["files_present"]
            s["crawl_limit"] = CRAWL_LIMIT  # 화면 진행률·탐색 종료 예상 계산용
            s["time_budget"] = TIME_BUDGET  # 화면의 '예상 소요 19~29분' 계산용
            s["validation_mode"] = "complete" if options["complete_validation"] else "bounded"
            s["out_dir"] = str(app_dir())
            return self.send(200, s)
        if path == "/api/runs":
            return self.send(200, list_runs())
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
            if parts[3:] in (["result.json"], ["result_extra.json"]):
                # 탐지 엔진이 저장한 요강 붙임4 형식 파일을 바이트 그대로 전달 (재생성하지 않음).
                # report.json(대형 사이트 900MB)을 읽지 않는다 — counselpsy 10-22에서 2.4MB 내려받기에 6.8초
                name = parts[3]
                d = run_dir(parts[2])
                if not d:
                    return self.send(404, {"error": "점검 이력을 찾을 수 없습니다."})
                if not (d / name).is_file():
                    return self.send(404, {"error": f"이 점검의 {name}이 없습니다."})
                return self.send(200, (d / name).read_bytes(), "application/json; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="{name}"'})
            rep = load_run(parts[2])
            if rep is None:
                return self.send(404, {"error": "점검 이력을 찾을 수 없습니다."})
            if len(parts) == 3:
                view = parse_qs(urlparse(self.path).query).get("view", [])
                rep["has_extra"] = (HISTORY / parts[2] / "result_extra.json").is_file()  # 화면의 result_extra.json 버튼 표시용
                return self.send(200, ui_report(rep) if view == ["ui"] else rep)
            result_path = HISTORY / parts[2] / "result.json"
            if parts[3:] == ["export.csv"]:
                result_doc = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
                return self.send(200, export_csv(rep, result_doc), "text/csv; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="ad-purifier-{parts[2]}.csv"'})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path == "/api/shutdown":
            with state_lock:
                if not getattr(self.server, "closing", False):
                    self.server.closing = True
                    state.update(cancelling=True, phase="closing")
                    cancel_event.set()
                    self.server.shutdown_worker = threading.Thread(target=shutdown_server, args=(self.server,),
                                                                   name="app-shutdown")
                    self.server.shutdown_worker.start()
            return self.send(202, {"ok": True, "status": "closing"})
        if path == "/api/scan":
            req = self.body()
            url = (req.get("url") or "").strip()
            if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                return self.send(400, {"error": "http:// 또는 https:// 로 시작하는 URL을 입력하세요."})
            use_ml = bool(req.get("use_ml", True))
            if use_ml and not model_readiness()["files_present"]:
                return self.send(409, {"error": "AI 자산(Laya·Gemma) 또는 의존 파일이 없습니다. 설치 상태를 확인하세요."})
            with state_lock:
                if getattr(self.server, "closing", False):
                    return self.send(409, {"error": "프로그램 종료 중입니다."})
                previous_worker = getattr(self.server, "scan_worker", None)
                if state["running"] or (previous_worker is not None and previous_worker.is_alive()):
                    return self.send(409, {"error": "이미 탐지가 진행 중입니다."})
                state.update(running=True, phase="start", entry_url=url, pages_done=0, pages_found=1, current_url=url,
                             started=time.time(), run_id=None, error=None, cancelling=False,
                             ai_progress=None)
                cancel_event.clear()
                self.server.scan_worker = threading.Thread(target=scan_job, args=(url, use_ml),
                                                           name="scan-worker")
                self.server.scan_worker.start()
            return self.send(202, {"ok": True})
        if path == "/api/scan/cancel":
            with state_lock:
                if not state["running"]:
                    return self.send(409, {"error": "진행 중인 탐지가 없습니다."})
                state.update(cancelling=True, phase="cancelling")
                cancel_event.set()
            return self.send(202, {"ok": True})
        if path == "/api/history/clear":
            with state_lock:
                n = delete_runs([r["id"] for r in list_runs()])
            return self.send(200, {"ok": True, "deleted": n})
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "delete":
            with state_lock:
                n = delete_runs([parts[2]])
            if not n:
                return self.send(404, {"error": "점검 이력을 찾을 수 없습니다."})
            return self.send(200, {"ok": True, "deleted": n})
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "review":
            d = run_dir(parts[2])
            if not d:
                return self.send(404, {"error": "점검 이력을 찾을 수 없습니다."})
            req = self.body()
            key, status = str(req.get("key", "")), req.get("status", "new")
            if status not in REVIEW_STATES or not CAND_KEY.fullmatch(key):
                return self.send(400, {"error": "잘못된 요청"})
            review_path = d / "review.json"
            with state_lock:
                review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
                review[key] = {"status": status, "memo": str(req.get("memo", ""))[:1000],
                               "updated_at": datetime.now(KST).isoformat(timespec="seconds")}
                review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def main():
    bootstrap()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--complete-validation", action=argparse.BooleanOptionalAction, default=False,
                    help="기본: 전체 30분 안에 끝나도록 AI 판정에도 시간 상한 적용; --complete-validation이면 수집된 항목의 AI 판정을 시간 제한 없이 완료(실험)")
    args = ap.parse_args()
    options["complete_validation"] = args.complete_validation
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        raise RuntimeError(f"요청한 포트 {args.port}에서 서버를 시작할 수 없습니다. "
                           f"포트가 이미 사용 중인지 또는 바인딩 권한이 있는지 확인하세요. 원인: {exc}") from exc
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"광고청정기 실행 중: {url}  (종료: Ctrl+C)")
    print(f"결과 파일 위치: {app_dir()}")
    if args.complete_validation:
        print("[완료 우선] 수집된 항목의 AI 판정에는 시간 제한이 없습니다.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with state_lock:
            httpd.closing = True
            cancel_event.set()
            closer = getattr(httpd, "shutdown_worker", None)
        if closer is not None:
            closer.join()
        else:
            join_scan_worker(httpd)
        httpd.server_close()


if __name__ == "__main__":
    main()
