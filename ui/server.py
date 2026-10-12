"""광고청정기 화면: 로컬 웹 서버 + 브라우저 UI.

실행하면 127.0.0.1에 서버를 띄우고 기본 브라우저로 화면을 연다.
탐지 결과는 요강대로 구동 폴더(app_dir)에 result.json/report.json으로 쓰고,
점검 이력은 history/<실행시각>/ 에 report.json, result.json, review.json(검토 상태·메모)으로 쌓는다.
이력 삭제는 history/ 아래 실행 ID 형식 폴더만 지운다 (구동 폴더의 제출용 result.json은 그대로).

사용: python -m ui.server [--port 8780] [--no-browser]
"""
import argparse
import csv
import hashlib
import io
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
from urllib.parse import urlparse

from detector.main import DEFAULT_MODEL, JUDGE_RESERVE, KST, LLM_RESERVE, TIME_BUDGET, app_dir, run

CRAWL_LIMIT = TIME_BUDGET - LLM_RESERVE - JUDGE_RESERVE  # 화면 진행률에 쓰는 크롤링 최대 시간(초)

STATIC = Path(__file__).resolve().parent / "static"
HISTORY = app_dir() / "history"
RUN_ID = re.compile(r"\d{8}-\d{6}")
CAND_KEY = re.compile(r"[0-9a-f]{12}")
REVIEW_STATES = {"new", "confirmed", "false_positive", "resolved"}

state_lock = threading.Lock()
state = {"running": False, "phase": "idle", "pages_done": 0, "pages_found": 0, "current_url": "",
         "started": None, "run_id": None, "error": None}


def cand_key(c):
    return hashlib.sha1(f"{c['url']}|{c['location']}|{c['technique']}".encode()).hexdigest()[:12]


def scan_job(entry_url, use_ml, max_pages):
    def progress(done, found, url):
        with state_lock:
            state.update(pages_done=done, pages_found=found, current_url=url)

    def phase(name):
        with state_lock:
            state["phase"] = name

    run_id = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    try:
        run(entry_url, app_dir(), max_pages=max_pages, use_ml=use_ml, progress=progress, phase=phase)
        dest = HISTORY / run_id
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("result.json", "report.json"):
            shutil.copy2(app_dir() / name, dest / name)
        with state_lock:
            state.update(phase="done", run_id=run_id)
    except Exception as e:
        traceback.print_exc()
        with state_lock:
            state.update(phase="error", error=f"{type(e).__name__}: {e}")
    finally:
        with state_lock:
            state["running"] = False


def list_runs():
    runs = []
    for d in sorted(HISTORY.glob("*/report.json"), reverse=True):
        try:
            rep = json.loads(d.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        review_path = d.parent / "review.json"
        try:
            review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            review = {}
        ads = [c for c in rep["candidates"] if c["verdict"] == "ad"]
        # 처리 필요 = 미검토 + 불법광고 확인 (화면의 광고 청정도 등급 계산용)
        pending_ads = [c for c in ads if review.get(cand_key(c), {}).get("status", "new") in ("new", "confirmed")]
        runs.append({"id": d.parent.name, "entry_url": rep["meta"]["entry_url"],
                     "started_at": rep["meta"]["started_at"], "elapsed_sec": rep["meta"]["elapsed_sec"],
                     "pages": rep["pages"], "findings": len(ads), "pending": len(pending_ads),
                     "dirty_pages": len({c["url"] for c in pending_ads}),
                     "entry_failed": rep.get("entry_failed", False)})
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
    rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
    review_path = d / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
    for c in rep["candidates"]:
        c["key"] = cand_key(c)
        r = review.get(c["key"], {})
        c["review"] = r.get("status", "new")
        c["memo"] = r.get("memo", "")
    rep["id"] = run_id
    return rep


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
              "은닉 기법(한글)", "복원 문구", "판정 근거", "점수", "판정 주체", "검토 상태", "메모"]


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
            c["technique"], TECH_KO.get(c["technique"], c["technique"]), c["normalized"], ", ".join(c["hits"]), c["score"],
            "AI 보조 판정" if c["decided_by"] == "laya" else "규칙", REVIEW_KO.get(c["review"], c["review"]), c["memo"])])
    # 엑셀에서 한글이 깨지지 않도록 BOM 추가 (검토용 파일 — 제출용 result.json은 BOM 없음)
    return "\ufeff" + buf.getvalue()


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
            s["model_available"] = DEFAULT_MODEL.is_dir()
            s["crawl_limit"] = CRAWL_LIMIT
            s["out_dir"] = str(app_dir())
            return self.send(200, s)
        if path == "/api/runs":
            return self.send(200, list_runs())
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
            rep = load_run(parts[2])
            if rep is None:
                return self.send(404, {"error": "점검 이력을 찾을 수 없습니다."})
            if len(parts) == 3:
                return self.send(200, rep)
            result_path = HISTORY / parts[2] / "result.json"
            if parts[3:] == ["export.csv"]:
                result_doc = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
                return self.send(200, export_csv(rep, result_doc), "text/csv; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="ad-purifier-{parts[2]}.csv"'})
            if parts[3:] == ["result.json"]:
                # 탐지 엔진이 저장한 요강 붙임4 형식 파일을 바이트 그대로 전달 (재생성하지 않음)
                if not result_path.is_file():
                    return self.send(404, {"error": "이 점검의 result.json이 없습니다."})
                return self.send(200, result_path.read_bytes(), "application/json; charset=utf-8",
                                 {"Content-Disposition": 'attachment; filename="result.json"'})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path == "/api/scan":
            req = self.body()
            url = (req.get("url") or "").strip()
            if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                return self.send(400, {"error": "http:// 또는 https:// 로 시작하는 URL을 입력하세요."})
            use_ml = bool(req.get("use_ml", True)) and DEFAULT_MODEL.is_dir()
            try:
                max_pages = max(0, min(int(req.get("max_pages") or 0), 20000))  # 0 = 제한 없음(시간 예산까지 전부)
            except (TypeError, ValueError):
                return self.send(400, {"error": "최대 페이지는 숫자로 입력하세요."})
            with state_lock:
                if state["running"]:
                    return self.send(409, {"error": "이미 탐지가 진행 중입니다."})
                state.update(running=True, phase="start", pages_done=0, pages_found=1, current_url=url,
                             started=time.time(), run_id=None, error=None)
            threading.Thread(target=scan_job, args=(url, use_ml, max_pages), daemon=True).start()
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
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:  # 포트가 사용 중이면 빈 포트로
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"광고청정기 실행 중: {url}  (종료: Ctrl+C)")
    print(f"결과 파일 위치: {app_dir()}")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
