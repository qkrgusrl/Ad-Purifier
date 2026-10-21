"""REVISED01a1583f: 공식 진입점 계약. RED 실행은 부모 담당.

OS/frozen은 mock 검증이며 해당 OS에서의 실제 실행 증거가 아니다.
실제 소켓, 브라우저, 모델 추론 없이 공개 진입점과 Handler를 호출한다.
"""
import argparse
import importlib.machinery
import io
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import app
import app_paths
from ui import server as ui


class StandardEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="standard-entry-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.dict(ui.options, ui.options.copy(), clear=True))
        self.enterContext(patch.dict(ui.state, ui.state.copy(), clear=True))
        was_cancelled = ui.cancel_event.is_set()
        self.addCleanup(ui.cancel_event.set if was_cancelled else ui.cancel_event.clear)
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("프로세스 실행 금지")))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("네트워크 금지")))
        self.enterContext(patch.object(socket.socket, "bind", side_effect=AssertionError("소켓 bind 금지")))

    def ui_main(self, flags):
        httpd = Mock(server_address=("127.0.0.1", 8780), scan_worker=None, shutdown_worker=None)
        with patch.object(ui, "ThreadingHTTPServer", return_value=httpd), \
                patch.object(sys, "argv", ["ui.server", "--no-browser", *flags]), \
                patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
            ui.main()
        httpd.serve_forever.assert_called_once_with()
        return ui.options.copy()

    def test_app_forwards_default_false_and_explicit_boolean_choices(self):
        for flags, expected in (([], False), (["--complete-validation"], True),
                                (["--no-complete-validation"], False)):
            with self.subTest(flags=flags):
                received = []
                with patch.object(app, "bootstrap") as boot, patch.object(app, "ensure_writable_output"), \
                        patch.object(ui, "main", side_effect=lambda: received.extend(sys.argv[1:])):
                    original = sys.argv
                    self.assertEqual(app.main(["--no-browser", *flags]), 0)
                    self.assertIs(sys.argv, original)
                boot.assert_called_once_with()
                parser = argparse.ArgumentParser()
                parser.add_argument("--no-browser", action="store_true")
                parser.add_argument("--port", type=int)
                parser.add_argument("--complete-validation", action=argparse.BooleanOptionalAction, default=None)
                self.assertIs(parser.parse_args(received).complete_validation, expected,
                              "false도 UI에 명시 전달해야 함")

    def test_ui_default_bounded_and_explicit_complete_keep_collection_budget(self):
        for flags, expected in (([], False), (["--complete-validation"], True),
                                (["--no-complete-validation"], False)):
            with self.subTest(flags=flags):
                actual = self.ui_main(flags)
                self.assertIs(actual["complete_validation"], expected)
                self.assertIs(actual["time_limit"], True)

    def test_ui_main_calls_common_bootstrap_before_server_creation(self):
        events = []
        # 기존 import 별칭과 함수 내부 import 양쪽에 같은 공개 bootstrap spy를 제공한다.
        with patch.object(app_paths, "bootstrap", side_effect=lambda: events.append("bootstrap")) as boot, \
                patch.object(ui, "bootstrap", boot, create=True), \
                patch.object(ui, "ThreadingHTTPServer") as factory, \
                patch.object(sys, "argv", ["ui.server", "--no-browser"]), \
                patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
            httpd = Mock(server_address=("127.0.0.1", 8780), scan_worker=None, shutdown_worker=None)
            factory.side_effect = lambda *a, **kw: (events.append("server") or httpd)
            ui.main()
        self.assertEqual(events, ["bootstrap", "server"])

    def test_explicit_port_collision_reports_port_without_fallback_or_browser(self):
        # 첫 bind만 실패하고 두 번째는 성공하도록 해 자동 포트 우회를 검출한다.
        fallback = Mock(server_address=("127.0.0.1", 55306),
                        scan_worker=None, shutdown_worker=None)
        with patch.object(app_paths, "bootstrap") as boot, \
                patch.object(ui, "bootstrap", boot, create=True), \
                patch.object(ui, "ThreadingHTTPServer",
                             side_effect=[OSError("Address already in use"), fallback]) as factory, \
                patch.object(ui.threading, "Timer") as timer, \
                patch.object(ui.webbrowser, "open") as browser, \
                patch.object(sys, "argv", ["ui.server", "--port", "8780"]), \
                patch.object(sys, "stdout", io.StringIO()), \
                patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises((RuntimeError, OSError)) as raised:
                ui.main()
            message = str(raised.exception)
            self.assertIn("8780", message)
            self.assertRegex(message.lower(), r"사용\s*중|이미.*사용|already.*(?:use|occupied)|address.*in use")
            factory.assert_called_once_with(("127.0.0.1", 8780), ui.Handler)
            timer.assert_not_called()
            browser.assert_not_called()
            fallback.serve_forever.assert_not_called()

    def test_api_ignores_page_fields_and_preserves_ai_worker_route(self):
        for page_fields in ({}, {"max_pages": 3}):
            with self.subTest(fields=page_fields):
                handler = object.__new__(ui.Handler)
                handler.path = "/api/scan"
                handler.server = types.SimpleNamespace(closing=False, scan_worker=None)
                handler.trusted = lambda: True
                handler.body = lambda: {"url": "https://fixture.invalid/", **page_fields}
                handler.send = Mock()
                ui.state["running"] = False
                with patch.object(ui, "model_readiness", return_value={"files_present": True}), \
                        patch.object(ui.threading, "Thread") as worker:
                    handler.do_POST()
                self.assertEqual(worker.call_args.kwargs["args"], ("https://fixture.invalid/", True))
                self.assertIs(worker.call_args.kwargs["target"], ui.scan_job)
                worker.return_value.start.assert_called_once_with()
                self.assertEqual(handler.send.call_args.args[0], 202)

    def test_scan_job_forwards_ai_and_bounded_without_page_limit_or_inference(self):
        self.ui_main([])
        ui.cancel_event.clear()
        with patch.object(ui, "run", side_effect=ui.ScanCancelled) as run:
            ui.scan_job("https://fixture.invalid/", True)
        self.assertEqual(run.call_args.args[0], "https://fixture.invalid/")
        self.assertNotIn("max_pages", run.call_args.kwargs)
        self.assertEqual({k: run.call_args.kwargs[k] for k in ("use_ml", "complete_validation")},
                         {"use_ml": True, "complete_validation": False})
        self.assertNotIn("time_budget", run.call_args.kwargs)

    def test_bootstrap_preserves_explicit_browser_path_in_source_and_frozen(self):
        for frozen in (False, True):
            for explicit in ("0", str(self.root / "custom-browsers")):
                with self.subTest(frozen=frozen, explicit=explicit), \
                        patch.object(sys, "frozen", frozen, create=True), \
                        patch.object(app_paths, "app_root", return_value=self.root), \
                        patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": explicit}, clear=True):
                    app_paths.bootstrap()
                    self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], explicit)

    def test_bootstrap_source_browser_resolution_across_platforms(self):
        package = self.root / "venv/site-packages/playwright"
        package.mkdir(parents=True)
        package_file = package / "__init__.py"
        package_file.touch()
        spec = importlib.machinery.ModuleSpec("playwright", loader=None, origin=str(package_file), is_package=True)
        spec.submodule_search_locations = [str(package)]
        fake = types.ModuleType("playwright")
        fake.__file__, fake.__spec__, fake.__path__ = str(package_file), spec, [str(package)]
        local = package / "driver/package/.local-browsers"
        bundled = self.root / "runtime/chromium"
        for platform in ("darwin", "linux", "win32"):
            for mode in ("cache", "venv", "bundled"):
                with self.subTest(platform=platform, mode=mode):
                    if mode != "cache":
                        local.mkdir(parents=True, exist_ok=True)
                    if mode == "bundled":
                        bundled.mkdir(parents=True, exist_ok=True)
                    with patch.object(sys, "platform", platform), patch.object(sys, "frozen", False, create=True), \
                            patch.object(app_paths, "app_root", return_value=self.root), \
                            patch.dict(sys.modules, {"playwright": fake}), patch.dict(os.environ, {}, clear=True):
                        app_paths.bootstrap()
                        self.assertEqual(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
                                         {"cache": None, "venv": "0", "bundled": str(bundled)}[mode])
                    if bundled.exists():
                        bundled.rmdir()
                    if local.exists():
                        local.rmdir()

    def test_readiness_uses_files_and_dependencies_without_loading_models(self):
        laya = self.root / "models/laya-illegal-ad-long2"
        for name in ("model.safetensors", "rl_agent_config.json", "questions.json",
                     "encoder/config.json", "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json"):
            path = laya / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture-not-model-weights")
        gemma, llama = self.root / "model.gguf", self.root / "llama-server"
        gemma.write_bytes(b"fixture-not-gguf")
        llama.write_bytes(b"fixture-not-executable")
        with patch.object(ui, "DEFAULT_MODEL", laya), patch.object(ui, "gemma_model", return_value=gemma), \
                patch.object(ui, "llama_server", return_value=llama), patch.object(sys, "platform", "linux"), \
                patch.object(ui.importlib.util, "find_spec", return_value=object()), \
                patch.object(ui, "run", side_effect=AssertionError("readiness에서 추론 금지")) as run:
            ready = ui.model_readiness()
            self.assertIs(ready["files_present"], True)
            self.assertEqual(ready["execution_status"], "not_checked")
            gemma.unlink()
            self.assertIs(ui.model_readiness()["files_present"], False)
            run.assert_not_called()

    def test_frozen_browser_and_canonical_model_paths_ignore_cwd(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(self.root / "AdPurifier.exe")), \
                patch.object(sys, "_MEIPASS", str(self.root / "internal"), create=True):
            app_paths.bootstrap()
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(self.root / "runtime/chromium"))
            self.assertEqual(app_paths.laya_model(), self.root / "models/laya-illegal-ad-long2")
            self.assertEqual(app_paths.gemma_model(), self.root / "models/gemma/gemma-4-E2B_q4_0-it.gguf")
            with patch.dict(os.environ, {"AD_PURIFIER_MODEL": "custom/laya", "AD_PURIFIER_GEMMA_MODEL": "custom/gemma.gguf"}):
                self.assertEqual(app_paths.laya_model(), self.root / "custom/laya")
                self.assertEqual(app_paths.gemma_model(), self.root / "custom/gemma.gguf")


if __name__ == "__main__":
    unittest.main()
