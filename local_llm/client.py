"""점검 전체에서 하나의 서버와 시간/취소 예산을 공유한다."""
import uuid

from .engine import Budget, LocalServer
from .prompts import DEFAULT_VERSION
from .prototype import run_extract, run_verify


class GemmaClient:
    def __init__(self, *, deadline=None, cancel=None, threads=None, ctx_size=4096, log_dir=None, on_event=None,
                 threads_batch=None, ubatch_size=None):
        self.on_event = on_event
        self.budget = Budget(deadline, cancel)
        self.options = dict(log_dir=log_dir, threads=threads, threads_batch=threads_batch,
                            ubatch_size=ubatch_size, ctx_size=ctx_size, budget=self.budget, on_event=self._event)
        self.server = None

    def __enter__(self):
        if self.server is not None:
            raise RuntimeError('이미 열린 GemmaClient')
        self.budget.remaining()
        server = LocalServer(**self.options)
        server.__enter__()
        self.server = server
        return self

    def __exit__(self, *exc):
        server, self.server = self.server, None
        if server is not None:
            server.__exit__(*exc)

    def _active(self):
        self.budget.remaining()
        if self.server is None:
            raise RuntimeError('GemmaClient는 with 문 안에서 사용해야 함')
        return self.server

    def _event(self, event):
        observer = getattr(self, "on_event", None)
        if observer is not None:
            try:
                observer(dict(event))
            except Exception:
                try:
                    observer({"event": "audit_error"})
                except Exception:
                    pass

    def verify(self, data):
        return run_verify(self._active(), data, DEFAULT_VERSION)

    def extract(self, data):
        preflight_reported = False

        def on_preflight(owners):
            nonlocal preflight_reported
            preflight_reported = True
            for owner in owners:
                self._event({"event": "item_end", "request_id": uuid.uuid4().hex,
                             "candidate_id": owner, "task": "verify", "status": "preflight_failed",
                             "completion_received": False, "request_count": 0, "preflight_failed": 1})

        result = run_extract(self._active(), data, DEFAULT_VERSION, on_preflight=on_preflight)
        # 사전 통지를 받은 실행은 사후 fallback에서 중복 종료하지 않는다.
        if not preflight_reported:
            for failure in result.get("failures", []):
                error = failure.get("error")
                if (failure.get("preflight_failed") or error == "input_overflow"
                        or isinstance(error, str) and error.startswith("preflight_error:")):
                    on_preflight(failure.get("block_ids", []))
        return result
