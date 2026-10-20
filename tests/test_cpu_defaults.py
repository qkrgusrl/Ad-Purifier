"""CPU 스레드 자동 선택 계약. 실제 모델/llama-server는 실행하지 않는다."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
THREAD_ENV = (
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS",
)


def report_environment_target(conn, model_path):
    """실제 spawn 자식에서 환경을 읽는다. 부모 stdout/dup2는 건드리지 않는다."""
    conn.send_bytes(json.dumps({"status": "ready"}).encode())
    conn.recv_bytes()
    conn.send_bytes(json.dumps({
        "status": "ok", "environment": {key: os.environ.get(key) for key in THREAD_ENV},
        "pid": os.getpid(),
    }).encode())


class CpuDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), str(ROOT / "tests"), *sys.path]))
        self.enterContext(patch.dict(os.environ, {
            key: value for key, value in os.environ.items()
            if key not in ('AD_PURIFIER_GEMMA_THREADS', 'AD_PURIFIER_GEMMA_THREADS_BATCH',
                           'AD_PURIFIER_GEMMA_UBATCH_SIZE')
        }, clear=True))

    def test_laya_spawn_preserves_unset_and_explicit_thread_environment(self):
        runtime = importlib.import_module("detector.laya_runtime")
        baseline = {key: value for key, value in os.environ.items() if key not in THREAD_ENV}
        for value in (None, "4"):
            with self.subTest(value=value):
                env = dict(baseline)
                if value is not None:
                    env.update(dict.fromkeys(THREAD_ENV, value))
                with patch.dict(os.environ, env, clear=True):
                    with runtime.LayaRunner(
                        "unused-model", target=report_environment_target,
                        deadline=time.perf_counter() + 15,
                    ) as runner:
                        response = runner._exchange({"op": "environment"})
                    self.assertNotEqual(response["pid"], os.getpid())
                    self.assertEqual(response["environment"], dict.fromkeys(THREAD_ENV, value))

    def test_laya_worker_uses_cpu_agent_without_setting_torch_threads(self):
        runtime = importlib.import_module("detector.laya_runtime")
        torch = types.ModuleType("torch")
        torch.set_num_threads = Mock()
        torch.set_num_interop_threads = Mock()
        agent_module = types.ModuleType("laya.agent")
        agent_module.Agent = Mock()
        agent_module.Agent.return_value.predict_batch.return_value = [{"score": .5}]
        conn = Mock()
        request = {"op": "predict", "texts": ["문구"], "questions": ["질문"], "batch_size": 1}
        with patch.dict(sys.modules, {"torch": torch, "laya": types.ModuleType("laya"),
                                      "laya.agent": agent_module}), patch.object(
            runtime, "_receive_message", side_effect=[request, EOFError()]
        ):
            with self.assertRaises(EOFError):
                runtime._model_worker(conn, "unused-model")
        agent_module.Agent.assert_called_once_with("unused-model", device="cpu")
        agent_module.Agent.return_value.predict_batch.assert_called_once_with(
            request["texts"], request["questions"], batch_size=1)
        torch.set_num_threads.assert_not_called()
        torch.set_num_interop_threads.assert_not_called()

    def gemma_command(self, **options):
        engine = importlib.import_module("local_llm.engine")
        with tempfile.TemporaryDirectory() as directory:
            server = engine.LocalServer(log_dir=directory, **options)
            # __enter__의 명령 조립은 실제로 실행하되 transport/monitor만 대체한다.
            proc = Mock()
            proc.poll.return_value = None
            sock = Mock()
            sock.__enter__ = Mock(return_value=sock)
            sock.__exit__ = Mock(return_value=False)
            sock.getsockname.return_value = ("127.0.0.1", 12345)
            with patch.object(engine.Path, "is_file", return_value=True), patch.object(
                engine.socket, "socket", return_value=sock
            ), patch.object(engine.subprocess, "Popen", return_value=proc) as popen, patch.object(
                engine.threading, "Thread"
            ), patch.object(server, "request", return_value={"status": "ok"}):
                with server:
                    command = list(popen.call_args.args[0])
            self.assertEqual(command[command.index("--device") + 1], "none")
            self.assertEqual(command[command.index("--gpu-layers") + 1], "0")
            self.assertIn("--no-op-offload", command)
            self.assertIn("--no-kv-offload", command)
            return server.threads, command

    def test_gemma_defaults_to_none_and_omits_both_thread_flags(self):
        client = importlib.import_module("local_llm.client")
        threads, command = self.gemma_command()
        with self.subTest(component="LocalServer"):
            self.assertIsNone(threads)
        with self.subTest(component="command"):
            self.assertNotIn("--threads", command)
            self.assertNotIn("--threads-batch", command)
        with patch.object(client, "LocalServer") as factory:
            with client.GemmaClient():
                pass
            self.assertIsNone(factory.call_args.kwargs["threads"])

    def test_gemma_explicit_four_reaches_both_flags_and_client(self):
        client = importlib.import_module("local_llm.client")
        threads, command = self.gemma_command(threads=4)
        self.assertEqual(threads, 4)
        for flag in ("--threads", "--threads-batch"):
            self.assertEqual(command.count(flag), 1)
            self.assertEqual(command[command.index(flag) + 1], "4")
        with patch.object(client, "LocalServer") as factory:
            with client.GemmaClient(threads=4):
                pass
            self.assertEqual(factory.call_args.kwargs["threads"], 4)

    def test_gemma_separates_generation_and_prefill_settings(self):
        client = importlib.import_module("local_llm.client")
        _, command = self.gemma_command(threads=4, threads_batch=8, ubatch_size=256)
        for flag, value in (("--threads", "4"), ("--threads-batch", "8"), ("--ubatch-size", "256")):
            self.assertEqual(command.count(flag), 1)
            self.assertEqual(command[command.index(flag) + 1], value)
        with patch.object(client, "LocalServer") as factory:
            with client.GemmaClient(threads=4, threads_batch=8, ubatch_size=256):
                pass
            self.assertEqual(factory.call_args.kwargs["threads_batch"], 8)
            self.assertEqual(factory.call_args.kwargs["ubatch_size"], 256)

    def test_gemma_environment_options_and_explicit_precedence(self):
        with patch.dict(os.environ, {'AD_PURIFIER_GEMMA_THREADS': '3',
                                    'AD_PURIFIER_GEMMA_THREADS_BATCH': '8',
                                    'AD_PURIFIER_GEMMA_UBATCH_SIZE': '256'}):
            _, command = self.gemma_command()
            self.assertEqual(command[command.index('--threads') + 1], '3')
            self.assertEqual(command[command.index('--threads-batch') + 1], '8')
            self.assertEqual(command[command.index('--ubatch-size') + 1], '256')
            _, command = self.gemma_command(threads=4, threads_batch=4, ubatch_size=128)
            self.assertEqual(command[command.index('--threads') + 1], '4')
            self.assertEqual(command[command.index('--threads-batch') + 1], '4')
            self.assertEqual(command[command.index('--ubatch-size') + 1], '128')

    def test_gemma_rejects_invalid_tuning_before_starting_model(self):
        engine = importlib.import_module('local_llm.engine')
        for options in ({'threads_batch': 0}, {'threads_batch': True}, {'threads_batch': 1.5},
                        {'ubatch_size': -1}, {'ubatch_size': 513}, {'threads': 'bad'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                engine.LocalServer(**options)
