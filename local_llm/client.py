"""점검 전체에서 하나의 서버와 시간/취소 예산을 공유한다."""
from .engine import Budget, LocalServer
from .prompts import DEFAULT_VERSION
from .prototype import run_extract, run_verify


class GemmaClient:
    def __init__(self, *, deadline=None, cancel=None, threads=None, ctx_size=4096, log_dir=None):
        self.budget = Budget(deadline, cancel)
        self.options = dict(log_dir=log_dir, threads=threads, ctx_size=ctx_size, budget=self.budget)
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

    def verify(self, data):
        return run_verify(self._active(), data, DEFAULT_VERSION)

    def extract(self, data):
        return run_extract(self._active(), data, DEFAULT_VERSION)
