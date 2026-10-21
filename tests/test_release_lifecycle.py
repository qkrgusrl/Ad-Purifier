"""릴리스 종료/취소 반례. 실제 모델/네트워크 없이 제품 공개 경로를 실행한다.

Laya target은 spawn에서 import 가능한 최상위 함수다. 종료 판정은 테스트의
비상 cleanup 전에 채취한다. 짧게 기다린 뒤 살아 있는 thread를 버리면 실패한다.
"""
import asyncio
import importlib
import io
import json
import multiprocessing
from multiprocessing.connection import _ConnectionBase
import os
from pathlib import Path
import socket
import sys
import threading
import time
import types
import unittest
import tempfile
from unittest.mock import Mock, patch

import psutil

ROOT = Path(__file__).resolve().parents[1]


def blocked_load_target(conn, model_path):
    Path(model_path, "entered").write_text(str(os.getpid()))
    threading.Event().wait()  # ready 전송 전의 동기 Agent load 모형


def blocked_batch_target(conn, model_path):
    conn.send_bytes(json.dumps({"status": "ready"}).encode("utf-8"))
    request = json.loads(conn.recv_bytes())
    if request['op'] == 'plan':
        conn.send_bytes(json.dumps({'status': 'ok', 'plans': [
            {'input_tokens': 1, 'token_limit': 1024, 'segments': [
                {'start_char': 0, 'end_char': len(text), 'input_tokens': 1}]}
            for text in request['texts']]}).encode('utf-8'))
        request = json.loads(conn.recv_bytes())
    assert request["op"] == "predict"
    Path(model_path, "entered").write_text(str(os.getpid()))
    threading.Event().wait()  # 요청 수신 후의 동기 batch 모형


def blocked_receive_target(conn, model_path):
    conn.send_bytes(json.dumps({"status": "ready"}).encode("utf-8"))
    Path(model_path, "entered").write_text(str(os.getpid()))
    threading.Event().wait()  # 요청을 읽지 않아 부모 send_bytes가 Pipe 용량에서 막힘


def echo_target(conn, model_path):
    Path(model_path, "entered").write_text(str(os.getpid()))
    conn.send_bytes(json.dumps({"status": "ready"}).encode("utf-8"))
    while True:
        request = json.loads(conn.recv_bytes())
        outputs = [{"answers": {"is_illegal_ad": {"noul": .55}}, "input": text,
                    "questions": request["questions"], "batch_size": request["batch_size"],
                    "usage": {"truncated": False}, "pid": os.getpid(),
                    "start_method": multiprocessing.get_start_method()} for text in request["texts"]]
        conn.send_bytes(json.dumps({"status": "ok", "outputs": outputs}, ensure_ascii=False).encode("utf-8"))


class ExistingLayaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), str(ROOT / "tests"), *sys.path]))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("외부 네트워크 금지")))
        self.enterContext(patch.object(socket.socket, "bind", side_effect=AssertionError("서버 실행 금지")))
        self.temp = tempfile.TemporaryDirectory(prefix="취소 반례 ")
        self.addCleanup(self.temp.cleanup)
        self.model_path = Path(self.temp.name)

    def blocked_analyze(self, stage, trigger):
        ai = importlib.import_module("detector.ai")
        main = importlib.import_module("detector.main")
        cancel, release, active = threading.Event(), threading.Event(), threading.Event()
        marker = self.model_path / "entered"
        outcome = {}
        baseline_children = {p.pid for p in multiprocessing.active_children()}

        def block():
            active.set()
            marker.write_text(str(os.getpid()))
            try:
                release.wait(10)  # harness 비상 회수; 성공 판정은 release 전에 수행
            finally:
                active.clear()

        class BlockingAgent:
            def __init__(self, *args, **kwargs):
                if stage == "load":
                    block()

            def predict_batch(self, texts, questions, batch_size=16):
                if stage == "batch":
                    block()
                return [{"answers": {"is_illegal_ad": {"noul": .5}}} for _ in texts]

        # 현재 동기 경로는 이 Agent에서 막힌다. 이식 후에는 실제 격리 runner에
        # target만 주입한다. 부모 Agent mock을 spawn child에 전파하지 않는다.
        def runner_factory(model_path, **kwargs):
            module = importlib.import_module("detector.laya_runtime")
            return module.LayaRunner(model_path, target=blocked_load_target if stage == "load" else blocked_batch_target,
                                     **kwargs)

        agent_module = types.ModuleType("laya.agent")
        agent_module.Agent = BlockingAgent
        laya_module = types.ModuleType("laya")
        laya_module.__path__ = []
        laya_module.agent = agent_module
        gemma_module = types.ModuleType("local_llm.client")
        gemma_module.GemmaClient = lambda **kwargs: self.fail("취소/기한 만료 뒤 Gemma 실행 금지")
        deadline = time.perf_counter() + (1.0 if trigger == "deadline" else 30)
        candidates = [{"source_id": "owner-1", "source_html": "<a>환전 안내</a>",
                       "source_context": "근접 문맥", "evidence_text": "환전 안내", "context": [],
                       "verdict": "borderline", "url": "https://fixture.invalid/", "location": "a"}]

        def invoke():
            try:
                outcome["result"] = ai.analyze(candidates, [], str(self.model_path), True,
                                                deadline=deadline, cancel=cancel, cancel_error=main.ScanCancelled)
            except BaseException as exc:
                outcome["error"] = exc

        with patch.dict(sys.modules, {"laya": laya_module, "laya.agent": agent_module,
                                      "local_llm.client": gemma_module}), \
                patch.object(ai, "LayaRunner", runner_factory, create=True):
            caller = threading.Thread(target=invoke, name="contract-analyze-caller", daemon=True)
            caller.start()
            try:
                until = time.monotonic() + 3
                while not marker.exists() and caller.is_alive() and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(marker.exists(), f"{stage} 블록에 도달하지 못함: {outcome}")
                pid = int(marker.read_text())
                if trigger == "cancel":
                    cancel.set()
                caller.join(timeout=max(0, deadline - time.perf_counter()) + 2 if trigger == "deadline" else 2)
                observed = {"caller_alive": caller.is_alive(), "parent_agent_active": active.is_set(),
                            "owned_child_alive": pid != os.getpid() and psutil.pid_exists(pid)}
            finally:
                # 현재 결함을 테스트 프로세스 누수로 남기지 않는다. 관측값은 위에서 고정.
                release.set()
                cancel.set()
                for child in multiprocessing.active_children():
                    if child.pid not in baseline_children:
                        child.terminate(); child.join(2)
                        if child.is_alive():
                            child.kill(); child.join(2)
                caller.join(2)
        self.assertEqual(observed, {"caller_alive": False, "parent_agent_active": False, "owned_child_alive": False},
                         f"{trigger} 뒤 동기 Laya {stage}가 회수되지 않음; 테스트 cleanup 전 관측")
        if trigger == "cancel":
            self.assertIsInstance(outcome.get("error"), (main.ScanCancelled, asyncio.CancelledError))
        else:
            self.assertEqual(candidates[0]["verdict"], "uncertain", "시간 초과를 정상/광고 확정으로 처리하면 안 됨")

    def test_existing_analyze_cancel_reclaims_blocked_load(self):
        self.blocked_analyze("load", "cancel")

    def test_existing_analyze_cancel_reclaims_blocked_batch(self):
        self.blocked_analyze("batch", "cancel")

    def test_existing_analyze_deadline_reclaims_blocked_load(self):
        self.blocked_analyze("load", "deadline")

    def test_existing_analyze_deadline_reclaims_blocked_batch(self):
        self.blocked_analyze("batch", "deadline")


class IsolatedLayaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), str(ROOT / "tests"), *sys.path]))
        self.temp = tempfile.TemporaryDirectory(prefix="격리 런타임 ")
        self.addCleanup(self.temp.cleanup)
        self.model_path = Path(self.temp.name)
        self.assertTrue((ROOT / "detector/laya_runtime.py").is_file(), "승인된 격리 LayaRunner 구현 부재")
        self.Runner = importlib.import_module("detector.laya_runtime").LayaRunner

    def blocked_runner(self, stage, trigger):
        target = {"load": blocked_load_target, "batch": blocked_batch_target, "send": blocked_receive_target}[stage]
        cancel = threading.Event()
        deadline = time.perf_counter() + (1.0 if trigger == "deadline" else 30)
        runner = self.Runner(str(self.model_path), deadline=deadline, cancel=cancel, target=target)
        before_threads = set(threading.enumerate())
        outcome = {}
        send_entered, send_finished = threading.Event(), threading.Event()
        real_send = _ConnectionBase.send_bytes

        def observed_send(conn, buffer, *args, **kwargs):
            large = len(buffer) > 1024 * 1024
            if large:
                send_entered.set()
            try:
                return real_send(conn, buffer, *args, **kwargs)
            finally:
                if large:
                    send_finished.set()

        if stage == "send":
            # POSIX Connection과 Windows PipeConnection이 함께 상속하는 송신 경계.
            self.enterContext(patch.object(_ConnectionBase, "send_bytes", observed_send))

        def invoke():
            try:
                with runner:
                    runner.predict_batch(["한" * (1024 * 1024) if stage == "send" else "원문"], {"q": "질문"})
            except BaseException as exc:
                outcome["error"] = exc

        caller = threading.Thread(target=invoke, name="contract-runner-caller", daemon=True)
        caller.start()
        try:
            marker = self.model_path / "entered"
            until = time.monotonic() + 3
            while not marker.exists() and caller.is_alive() and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue(marker.exists(), f"실제 spawn 자식이 {stage}에 도달하지 못함: {outcome}")
            pid = int(marker.read_text())
            self.assertNotEqual(pid, os.getpid())
            self.assertEqual(runner.pid, pid)
            if stage == "send":
                self.assertTrue(send_entered.wait(1), "부모가 실제 큰 프레임 송신에 진입하지 않음")
                self.assertFalse(send_finished.wait(.05), "Pipe 송신이 실제 블록된 상태에서 취소해야 함")
            if trigger == "cancel":
                cancel.set()
            caller.join(max(0, deadline - time.perf_counter()) + 2 if trigger == "deadline" else 2)
            observed = {"caller_alive": caller.is_alive(), "pid_alive": psutil.pid_exists(pid),
                        "new_threads": [t.name for t in threading.enumerate()
                                        if t not in before_threads and t is not caller]}
        finally:
            cancel.set()
            for child in multiprocessing.active_children():
                if child.pid == runner.pid:
                    child.terminate(); child.join(1)
                    if child.is_alive():
                        child.kill(); child.join(1)
            caller.join(2)
        self.assertEqual(observed, {"caller_alive": False, "pid_alive": False, "new_threads": []},
                         "종료 반환 전에 소유 자식/Pipe I/O thread를 모두 회수해야 함")
        self.assertIsInstance(outcome.get("error"), asyncio.CancelledError if trigger == "cancel" else TimeoutError)
        self.assertEqual(runner.pid, pid, "종료 후에도 실제 PID를 정리 증거로 유지해야 함")

    def test_spawn_load_cancel_kills_child_and_joins_io(self):
        self.blocked_runner("load", "cancel")

    def test_spawn_load_deadline_kills_child_and_joins_io(self):
        self.blocked_runner("load", "deadline")

    def test_spawn_batch_cancel_kills_child_and_joins_io(self):
        self.blocked_runner("batch", "cancel")

    def test_spawn_batch_deadline_kills_child_and_joins_io(self):
        self.blocked_runner("batch", "deadline")

    def test_spawn_blocked_pipe_send_cancel_kills_child_and_joins_io(self):
        self.blocked_runner("send", "cancel")

    def assert_pid_gone(self, pid):
        # Windows는 종료 핸들이 signaled된 뒤에도 PID 조회에 잠시 남을 수 있다.
        # psutil의 Windows wait도 이 OS 가시성 지연을 bounded polling한다.
        if os.name == "nt":
            try:
                psutil.Process(pid).wait(timeout=1)
            except psutil.NoSuchProcess:
                pass
        self.assertFalse(psutil.pid_exists(pid))

    def test_spawn_reuses_one_child_preserves_outputs_and_has_no_candidate_count_cap(self):
        before_threads = set(threading.enumerate())
        runner = self.Runner(str(self.model_path), deadline=time.perf_counter() + 10, target=echo_target)
        with runner:
            first = runner.predict_batch(["첫 원문"], {"q": "첫 질문"}, batch_size=16)
            pid = runner.pid
            texts = [f"후보 {i}" for i in range(1025)]
            second = runner.predict_batch(texts, {"q": "다음 질문"}, batch_size=64)
            self.assertEqual({r["pid"] for r in first + second}, {pid})
            self.assertEqual({r["start_method"] for r in first + second}, {"spawn"})
            self.assertEqual([r["input"] for r in second], texts)
            self.assertEqual(first[0]["answers"], {"is_illegal_ad": {"noul": .55}})
            self.assertEqual(second[-1]["usage"], {"truncated": False})
            with self.assertRaises(AttributeError):
                runner.pid = 1
        self.assert_pid_gone(pid)
        self.assertEqual(runner.pid, pid)
        self.assertEqual(set(threading.enumerate()) - before_threads, set())

    def test_spawn_oversized_frame_is_runtime_error_and_reclaims_child(self):
        before_threads = set(threading.enumerate())
        runner = self.Runner(str(self.model_path), deadline=time.perf_counter() + 10, target=echo_target)
        with self.assertRaises(RuntimeError):
            with runner:
                runner.predict_batch(["x" * (16 * 1024 * 1024 + 1)], {"q": "질문"})
        self.assertIsInstance(runner.pid, int)
        self.assert_pid_gone(runner.pid)
        self.assertEqual(set(threading.enumerate()) - before_threads, set())


class CleanupFailureForTest(RuntimeError):
    """새 예외 API가 없던 baseline도 실제 fallback 동작까지 실행하기 위한 모형."""


class LayaCleanupContractTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), *sys.path]))
        self.runtime = importlib.import_module("detector.laya_runtime")

    def stalled_runner(self, stage):
        runner = self.runtime.LayaRunner("fixture-only")
        process, thread, conn = Mock(), Mock(), Mock()
        process.pid = 424242
        process.is_alive.return_value = stage == "process"
        thread.ident = 1
        thread.is_alive.return_value = stage == "io"
        runner._process, runner._io_thread, runner._conn, runner._pid = process, thread, conn, process.pid
        return runner, process, thread

    def test_cleanup_failure_type_is_cached_without_repeating_stop_or_join(self):
        for stage in ("process", "io"):
            with self.subTest(stage=stage):
                runner, process, thread = self.stalled_runner(stage)
                with self.assertRaises(RuntimeError) as first:
                    runner.close()
                counts = (process.terminate.call_count, process.kill.call_count,
                          process.join.call_count, thread.join.call_count)
                with self.assertRaises(RuntimeError) as second:
                    runner.__exit__(RuntimeError, RuntimeError("prediction failed"), None)
                self.assertEqual((process.terminate.call_count, process.kill.call_count,
                                  process.join.call_count, thread.join.call_count), counts,
                                 "중첩 cleanup이 terminate/kill/join을 반복해 종료 bound를 누적함")
                self.assertIs(second.exception, first.exception)
                self.assertEqual(type(first.exception).__name__, "LayaCleanupError")
                self.assertIn("424242", str(first.exception))
                self.assertRegex(str(first.exception), "(?i)(process|child|자식)" if stage == "process" else "(?i)(io|thread|스레드)")

    def test_enter_load_error_plus_cleanup_failure_preserves_cleanup_error_type(self):
        runner, process, thread = self.stalled_runner("process")
        context = Mock()
        context.Pipe.return_value = (Mock(), Mock())
        context.Process.return_value = process
        with patch.object(self.runtime.multiprocessing, "get_context", return_value=context), \
                patch.object(runner, "_exchange", side_effect=RuntimeError("fixture load failed")):
            with self.assertRaises(RuntimeError) as failure:
                runner.__enter__()
        self.assertEqual(type(failure.exception).__name__, "LayaCleanupError",
                         "load 오류와 child 회수 실패가 겹쳐도 일반 RuntimeError로 fallback하면 안 됨")
        self.assertIn("424242", str(failure.exception))

    def analyze_failure(self, failure_stage):
        ai = importlib.import_module("detector.ai")
        CleanupError = getattr(self.runtime, "LayaCleanupError", CleanupFailureForTest)
        calls = []

        class FakeRunner:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                if failure_stage == "enter_cleanup":
                    raise CleanupError("pid=424242 child cleanup failed")
                if failure_stage == "load_runtime":
                    raise RuntimeError("load failed; child already reaped")
                return self

            def __exit__(self, *args):
                if failure_stage == "exit_cleanup":
                    raise CleanupError("pid=424242 io cleanup failed")

            def predict_batch(self, texts, questions, batch_size=16):
                if failure_stage == "predict_runtime":
                    raise RuntimeError("prediction failed; child cleanup succeeds")
                return [{"answers": {"is_illegal_ad": {"noul": .9}}} for _ in texts]

        class FakeGemma:
            def __init__(self, **kwargs):
                calls.append("construct")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def verify(self, data):
                calls.append("verify")
                return {"status": "needs_review", "results": []}

        gemma = types.ModuleType("local_llm.client")
        gemma.GemmaClient = FakeGemma
        candidates = [{"source_id": "source-1", "source_html": "<a>환전 안내</a>", "source_context": "근접 문맥",
                       "evidence_text": "환전 안내", "context": [], "verdict": "borderline"}]
        with patch.object(ai, "LayaRunner", FakeRunner), patch.dict(sys.modules, {"local_llm.client": gemma}):
            stats = ai.analyze(candidates, [], "fixture-only", True)
        return calls, candidates[0], stats

    def test_cleanup_failure_in_enter_or_exit_blocks_gemma_and_leaves_review(self):
        for stage in ("enter_cleanup", "exit_cleanup"):
            with self.subTest(stage=stage):
                calls, candidate, stats = self.analyze_failure(stage)
                self.assertEqual(calls, [], "Laya 자식/IPC 정리 실패인데 Gemma fallback을 시작함")
                self.assertEqual(candidate["verdict"], "uncertain")
                self.assertNotEqual(candidate["processing_status"], "completed")
                self.assertTrue(stats["failures"])

    def test_cleaned_runtime_errors_keep_review_without_gemma(self):
        for stage in ("load_runtime", "predict_runtime"):
            with self.subTest(stage=stage):
                calls, candidate, stats = self.analyze_failure(stage)
                self.assertEqual(calls, [], "유효 Laya 없는 runtime 오류는 Gemma 대상이 아님")
                self.assertEqual(candidate["verdict"], "uncertain")
                self.assertNotEqual(candidate["processing_status"], "completed")
                self.assertTrue(stats["failures"])


class UIShutdownLifecycleTests(unittest.TestCase):
    def test_ui_shutdown_endpoint_cancels_joins_then_closes_server_and_owned_runtime(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), *sys.path]))
        ui = importlib.import_module("ui.server")
        events, workers, replies = [], [], []
        entered, shutdown_requested = threading.Event(), threading.Event()
        real_thread = threading.Thread
        owned = {"alive": False}

        class CancelEvent:
            def __init__(self):
                self.event = threading.Event()

            def set(self):
                if not self.event.is_set():
                    events.append("cancel")
                self.event.set()

            def clear(self):
                self.event.clear()

            def is_set(self):
                return self.event.is_set()

            def wait(self, timeout=None):
                return self.event.wait(timeout)

        cancel = CancelEvent()

        def controlled_scan(*args, **kwargs):
            owned["alive"] = True
            events.append("runtime_started")
            entered.set()
            try:
                if not kwargs["cancel"].wait(5):
                    raise RuntimeError("fixture watchdog: 취소 신호 없음")
                raise ui.ScanCancelled()
            finally:
                owned["alive"] = False
                events.append("runtime_closed")

        class TrackedThread(real_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.scan_worker = kwargs.get("target") is ui.scan_job
                if self.scan_worker:
                    workers.append(self)

            def join(self, timeout=None):
                if self.scan_worker:
                    events.append("worker_join_started")
                super().join(timeout)
                if self.scan_worker and not self.is_alive():
                    events.append("worker_joined")

        case = self

        class ControlledHTTPServer:
            server_address = ("127.0.0.1", 8780)

            def __init__(self, *args, **kwargs):
                pass

            def post(self, path, body):
                handler = object.__new__(ui.Handler)
                handler.path, handler.command, handler.server = path, "POST", self
                handler.trusted = lambda: True
                handler.body = lambda: body
                handler.send = lambda code, payload, *args, **kwargs: replies.append((path, code, payload))
                handler.do_POST()

            def serve_forever(self):
                self.post("/api/scan", {"url": "https://fixture.invalid/", "use_ml": False})
                case.assertTrue(entered.wait(1), "실제 UI scan worker가 시작되지 않음")
                events.append("shutdown_endpoint")
                self.post("/api/shutdown", {})
                shutdown_requested.wait(1)

            def shutdown(self):
                events.append("server_shutdown")
                shutdown_requested.set()

            def server_close(self):
                events.append("server_closed")

        with patch.object(ui, "cancel_event", cancel), patch.dict(ui.state, {"running": False, "cancelling": False}), \
                patch.object(ui, "run", controlled_scan), patch.object(ui, "ThreadingHTTPServer", ControlledHTTPServer), \
                patch.object(ui.threading, "Thread", TrackedThread), patch.object(sys, "argv", ["app", "--no-browser"]), \
                patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("네트워크 금지")), \
                patch.object(socket.socket, "bind", side_effect=AssertionError("실제 HTTP 서버 금지")):
            try:
                ui.main()
                observed = {"endpoint_accepted": any(p == "/api/shutdown" and code in (200, 202) for p, code, _ in replies),
                            "cancelled": cancel.is_set(), "worker_joined": "worker_joined" in events,
                            "runtime_alive": owned["alive"], "server_closed": "server_closed" in events}
                before_cleanup_events = list(events)
            finally:
                # 모형 worker를 성공처럼 보이게 만드는 cleanup은 관측 이후에만 수행한다.
                cancel.set()
                for worker in workers:
                    real_thread.join(worker, 2)
        self.assertEqual(observed, {"endpoint_accepted": True, "cancelled": True, "worker_joined": True,
                                    "runtime_alive": False, "server_closed": True},
                         "정상 UI 종료가 active scan/소유 runtime/HTTP 서버를 회수하지 않음")
        order = [before_cleanup_events.index(name) for name in
                 ("shutdown_endpoint", "cancel", "runtime_closed", "worker_joined", "server_closed")]
        self.assertEqual(order, sorted(order), "취소→runtime 종료→worker join 완료→server close 순서 필요")


if __name__ == "__main__":
    unittest.main()
