"""windows-installer-20261018: 오프라인 수집/모델 더블. production 로직은 대체하지 않는다."""
import asyncio
import copy
import hashlib
import importlib
import json
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
URL = "https://fixture.invalid/article"

# runtime 모듈을 stub으로 교체하기 전에 실제 예외 타입을 보존한다.
# 이 모듈의 import는 모델 로딩이나 자식 프로세스 실행을 하지 않는다.
from detector.laya_runtime import LayaCleanupError


def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    if name in {"playwright", "laya", "local_llm"}:
        # 구현 예정인 engine/prompts/prototype의 import 경로를 가리지 않는다.
        value.__path__ = [str(ROOT / name)] if name == "local_llm" else []
    return value


# Playwright 설치/브라우저가 없어도 실제 run/candidates_from을 가져온다.
# 브라우저는 절대 실행하지 않는다. crawl만 아래에서 수집 더블로 주입한다.
_api = module("playwright.async_api", async_playwright=Mock(
    side_effect=AssertionError("테스트에서 브라우저 실행 금지")))
_agent = module("laya.agent", Agent=Mock(side_effect=AssertionError("초기 import 중 모델 실행 금지")))
_gemma = module("local_llm.client", GemmaClient=Mock(side_effect=AssertionError("초기 import 중 모델 실행 금지")))
with patch.dict(sys.modules, {"playwright": module("playwright", async_api=_api),
                              "playwright.async_api": _api,
                              "laya": module("laya", agent=_agent), "laya.agent": _agent,
                              "local_llm": module("local_llm", client=_gemma), "local_llm.client": _gemma}):
    main = importlib.import_module("detector.main")


def source(sid="s1", text="환전 안내", *, html=None, context="민원 게시판 원문", signals=None):
    selector = "p.article > a:nth-of-type(%s)" % (sid.lstrip("s") or "1")
    html = html if html is not None else '<a href="/offer/%s">%s</a>' % (sid, text)
    return {"source_id": sid, "source_html": html, "source_context": context,
            "selector": selector, "text": text, "raw": text, "tag": "a", "cls": "",
            "href": URL + "/" + sid,
            "signals": signals if signals is not None else [["TRANSPARENT", "opacity:0"]]}


def pages(rows, candidate_ids=None, *, prefix="", url=URL):
    candidate_ids = {r["source_id"] for r in rows} if candidate_ids is None else set(candidate_ids)
    return [(url, [(prefix, {"frame_url": url, "hidden": [copy.deepcopy(r) for r in rows
                            if r["source_id"] in candidate_ids], "texts": [], "links": [], "head": [],
                            "source_blocks": copy.deepcopy(rows)})])]


def prediction(p=0.55, **extra):
    return {"answers": {"is_illegal_ad": {"noul": p}}, **extra}


def bound_source(item):
    # bridge는 v15 sentence-ID를 원문 span으로 검증한 결과를 반환한다.
    raw = item["html"]
    # 고정 fixture의 태그 속성에 '>'가 있어도 본문 span은 정확히 유지한다.
    import re
    opening = re.match(r'<(?:[^\"\'>]|\"[^\"]*\"|\'[^\']*\')*>', raw)
    start, end = opening.end(), raw.rindex("<")
    owner = item.get("candidate_id", item.get("block_id"))
    digest = hashlib.sha256(json.dumps({k: item.get(k, "") for k in ("html", "context")},
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    binding = {"owner_id": owner, "evidence_id": "h0002", "source_field": "html",
               "start_char": start, "end_char": end, "source_sha256": digest}
    return {"evidence": raw[start:end], **binding}


def bound_reply(item, verdict="ad", status="ok"):
    """run_verify envelope. binding 필드는 results 항목에 평탄하게 놓인다."""
    parsed = {"verdict": verdict, **bound_source(item), "evidence_scope": "source_unit_selection"}
    row = {"candidate_id": item["candidate_id"], "status": "ok", **parsed,
           "needs_review": verdict == "uncertain" or status != "ok",
           "call": {"status": "ok", "parsed": copy.deepcopy(parsed), "request_count": 1}}
    return {"task": "verify", "version": "v15",
            "status": "needs_review" if row["needs_review"] else "ok", "results": [row]}


def extract_reply(data, candidates=None, failures=None, calls=None):
    """run_extract envelope. 실패의 소유 단위는 failures[].block_ids에 들어간다."""
    return {"task": "extract", "version": "v15", "status": "needs_review" if failures else "ok",
            "input_block_count": len(data["blocks"]), "candidates": candidates or [],
            "failures": failures or [], "calls": calls or [],
            "next_step": "후보 원문과 참조를 Laya 판정 흐름으로 전달"}


class OfflineRun:
    """Laya/Gemma만 교체. run, candidates_from, judge, dedupe, 파일 저장은 실제 코드."""
    def __init__(self, rows=None, candidate_ids=None, *, page_data=None, predict=None, verify=None, extract=None):
        self.rows = rows if rows is not None else [source()]
        self.page_data = page_data if page_data is not None else pages(self.rows, candidate_ids)
        self.laya_inputs, self.verify_inputs, self.extract_inputs, self.extract_requests = [], [], [], []
        self.events = []
        self.predict = predict or (lambda texts: [prediction() for _ in texts])
        self.verify_reply = verify or bound_reply
        self.extract_reply = extract or extract_reply
        self.entered = self.exited = 0
        self.model_paths = []
        self.laya_entered = self.laya_exited = 0
        self.clock = 100.0
        self.after_crawl = None
        harness = self

        class Agent:
            def __init__(self, *args, **kwargs):
                harness.model_paths.append(args[0] if args else kwargs.get("model_path"))

            def predict_batch(self, texts, *args, **kwargs):
                harness.laya_inputs.extend(texts)
                harness.events.append(("laya", list(texts)))
                return harness.predict(texts)

        class LayaRunner:
            def __init__(self, model_path, *, deadline=None, cancel=None, target=None):
                harness.model_paths.append(model_path)

            def __enter__(self):
                harness.laya_entered += 1
                return self

            def __exit__(self, *args):
                harness.laya_exited += 1
                return False

            def predict_batch(self, texts, questions, batch_size=16):
                harness.laya_inputs.extend(texts)
                harness.events.append(("laya", list(texts)))
                return harness.predict(texts)

        class GemmaClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                assert harness.laya_entered == harness.laya_exited, "LayaRunner context 종료 후 Gemma 진입 필요"
                harness.entered += 1
                return self

            def __exit__(self, *args):
                harness.exited += 1
                return False

            def verify(self, item):
                harness.verify_inputs.append(copy.deepcopy(item))
                harness.events.append(("verify", copy.deepcopy(item)))
                return harness.verify_reply(item)

            def extract(self, data):
                assert isinstance(data, dict) and isinstance(data.get("blocks"), list), \
                    "prototype 계약: extract({'blocks': [...]}) 필요"
                harness.extract_requests.append(copy.deepcopy(data))
                blocks = data["blocks"]
                harness.extract_inputs.extend(copy.deepcopy(blocks))
                harness.events.append(("extract", copy.deepcopy(blocks)))
                if len(harness.extract_inputs) > 20:
                    raise AssertionError("같은 source 재진입: extract 호출 상한 초과")
                return harness.extract_reply(data)

        self.Agent, self.LayaRunner, self.GemmaClient = Agent, LayaRunner, GemmaClient

    def __enter__(self):
        self.stack = ExitStack()
        # Windows Proactor의 socketpair는 connect를 사용한다. guard 전에 생성하고,
        # 실제 run에는 준비된 loop의 실행기만 주입한다. 전역 event-loop 정책은 유지한다.
        self.loop = asyncio.new_event_loop()
        self.stack.callback(self.loop.close)
        runtime_asyncio = types.SimpleNamespace(**vars(asyncio))
        runtime_asyncio.run = self.loop.run_until_complete
        self.stack.enter_context(patch.object(main, "asyncio", runtime_asyncio))
        la = module("laya.agent", Agent=self.Agent)
        lr = module("detector.laya_runtime", LayaRunner=self.LayaRunner, LayaCleanupError=LayaCleanupError)
        gc = module("local_llm.client", GemmaClient=self.GemmaClient)
        self.stack.enter_context(patch.dict(sys.modules, {
            "laya": module("laya", agent=la), "laya.agent": la,
            "detector.laya_runtime": lr,
            "local_llm": module("local_llm", client=gc), "local_llm.client": gc}))
        # 향후 ai 모듈이 모듈 최상단에서 import해도 매 사례의 더블을 사용한다.
        if (ROOT / "detector" / "ai.py").exists():
            ai = importlib.import_module("detector.ai")
            self.stack.enter_context(patch.object(ai, "LayaRunner", self.LayaRunner, create=True))
            self.stack.enter_context(patch.object(ai, "LayaCleanupError", LayaCleanupError, create=True))
            for name, value in (("Agent", self.Agent), ("GemmaClient", self.GemmaClient)):
                if hasattr(ai, name):
                    self.stack.enter_context(patch.object(ai, name, value))
        for name, value in (("Agent", self.Agent), ("LayaRunner", self.LayaRunner), ("GemmaClient", self.GemmaClient)):
            if hasattr(main, name):
                self.stack.enter_context(patch.object(main, name, value))

        async def crawl(*args, **kwargs):
            if self.after_crawl:
                self.after_crawl()
            return copy.deepcopy(self.page_data), {
                "failed": [], "http_errors": [], "external_skipped": [], "stopped_by_time": False,
                "left_in_queue": 0, "dropped_endless": 0, "skipped_utility": 0, "visit_log": [],
                "slow_mode": False, "retried": 0}

        self.stack.enter_context(patch.object(main, "crawl", AsyncMock(side_effect=crawl)))
        self.stack.enter_context(patch.object(main.time, "perf_counter", side_effect=lambda: self.clock))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("네트워크 금지")))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("네트워크 금지")))
        self.stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("외부 모델/프로세스 실행 금지")))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def run(self, out, **kwargs):
        options = {"model": "NONEXISTENT_TEST_WEIGHTS", "detect_posted": False, "time_budget": 600,
                   "llm_reserve": 60}
        options.update(kwargs)
        return main.run(URL, out, **options)


def recover(data):
    blocks = data["blocks"]
    return extract_reply(data, candidates=[{"block_id": b["block_id"], **bound_source(b)} for b in blocks],
                         calls=[{"status": "ok", "validation": {"accepted": [
                             {"block_id": b["block_id"], "evidence": bound_source(b)["evidence"]}
                             for b in blocks]}, "request_count": 1}])
