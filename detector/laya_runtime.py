"""점검별 Laya 자식 프로세스. 모델 로드와 예측 중에도 취소할 수 있다."""
import asyncio
import json
import multiprocessing
import os
import sys
import threading
import time


# 후보 개수 제한이 아닌 단일 IPC 메시지의 메모리 한도다. 초과 시 보류할 오류로 전달한다.
_MAX_PAYLOAD = 16 * 1024 * 1024
_POLL_SECONDS = .02
_JOIN_SECONDS = .3


class LayaCleanupError(RuntimeError):
    """모델 또는 통신 자원 회수를 확인할 수 없어 다음 모델을 열면 안 되는 오류."""


def _send_message(conn, message):
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > _MAX_PAYLOAD:
        raise RuntimeError("Laya IPC 메시지가 16 MiB 한도를 초과함")
    conn.send_bytes(payload)


def _receive_message(conn):
    # Connection.recv()의 pickle 역직렬화 대신 크기가 제한된 JSON만 받는다.
    message = json.loads(conn.recv_bytes(_MAX_PAYLOAD))
    if not isinstance(message, dict):
        raise RuntimeError("잘못된 Laya IPC 메시지")
    return message


def _preflight_limits(agent, texts, questions):
    """(state 토큰 수 목록, 최소 room, max_len, head_max_len). 판단 불가면 None(fail-open)."""
    if not texts or not questions or not all(isinstance(text, str) for text in texts):
        return None
    from laya.common import encode_text, serialize_state, state_room
    from laya.hooks import default_hooks

    # hook은 입력, 질문, 토큰 예산을 바꿀 수 있으므로 하나라도 있으면 검사를 생략한다.
    if default_hooks() or list(getattr(agent, "hooks", None) or ()):
        return None
    tok, cfg = agent.tok, agent.cfg
    max_len = int(cfg.get("max_len", 512))
    head_max_len = int(cfg.get("head_max_len", 192))
    room = min(state_room(tok, agent._to_internal(question), max_len, head_max_len)
               for question in questions.values())
    counts = [len(encode_text(tok, serialize_state(text).replace(tok.mask_token, " "),
                              add_special_tokens=False)["input_ids"]) for text in texts]
    return counts, room, max_len, head_max_len


def _checked_predict(agent, texts, questions, batch_size):
    """확실히 잘릴 입력은 forward 전에 빼고, 나머지만 predict_batch로 보내 원래 index로 합친다."""
    try:
        limits = _preflight_limits(agent, texts, questions)
    except Exception:
        limits = None  # 지원하지 않는 agent/tokenizer API는 기존 경로로 분류한다.
    if limits is None:
        return agent.predict_batch(texts, questions, batch_size=batch_size)
    counts, room, max_len, head_max_len = limits
    keep = [i for i, count in enumerate(counts) if count <= room]
    if len(keep) == len(texts):
        return agent.predict_batch(texts, questions, batch_size=batch_size)
    merged = [None] * len(texts)
    for i, count in enumerate(counts):
        if count > room:
            merged[i] = {"status": "preflight_rejected", "answers": {},
                         "usage": {"input_tokens": 0, "output_tokens": 0, "state_tokens": count},
                         "preflight": {"reason": "input_too_long", "input_tokens": count,
                                       "token_limit": room, "max_len": max_len,
                                       "head_max_len": head_max_len, "model_invoked": False}}
    if keep:
        outputs = agent.predict_batch([texts[i] for i in keep], questions, batch_size=batch_size)
        if not isinstance(outputs, list) or len(outputs) != len(keep):
            raise RuntimeError("Laya 예측 출력 개수 불일치")
        for i, output in zip(keep, outputs):
            merged[i] = output
    return merged


_SENTENCE_END = ".!?。！？…\n"
_MAX_VERIFY_STEPS = 256


class _PlanError(ValueError):
    def __init__(self, message, plan):
        super().__init__(message)
        self.plan = plan


def _segment_tokens(agent, text, questions):
    """정규화한 조각의 (token 수, room). 계획 불가면 ValueError."""
    from .textnorm import normalize

    limits = _preflight_limits(agent, [normalize(text)], questions)
    if limits is None:
        raise ValueError("token budget을 확정할 수 없음(hook 또는 미지원 tokenizer)")
    counts, room, _, _ = limits
    return counts[0], room


def _plan_one(agent, text, questions, max_chars):
    full_count, room = _segment_tokens(agent, text, questions)
    plan = {"input_tokens": full_count, "token_limit": room, "segments": []}
    if room <= 0:
        raise _PlanError("token budget이 0 이하라 분할할 수 없음", plan)
    size = len(text)
    if full_count <= room and size <= max_chars:
        plan["segments"].append({"start_char": 0, "end_char": size, "input_tokens": full_count})
        return plan
    start = 0
    while start < size:
        limit = min(size, start + max_chars)
        # 이분 탐색은 후보 위치를 찾는 용도일 뿐이고, 채택 전 조각을 다시 검증한다.
        low, high, best = start + 1, limit, start
        while low <= high:
            mid = (low + high) // 2
            count, mid_room = _segment_tokens(agent, text[start:mid], questions)
            if count <= mid_room:
                best, low = mid, mid + 1
            else:
                high = mid - 1
        candidates = []
        if best > start:
            floor = start + max(1, (best - start) // 2)
            window = range(best, floor - 1, -1)
            if best < size:
                candidates += [i for i in window if text[i - 1] in _SENTENCE_END]
                candidates += [i for i in window if text[i - 1].isspace() and i not in candidates]
            candidates.append(best)
        # 비단조 tokenization이면 best 아래로 내려가며 실제 통과하는 끝점을 찾는다.
        candidates += range(best - 1, max(start, best - _MAX_VERIFY_STEPS), -1)
        chosen = None
        for end in candidates:
            if end <= start:
                continue
            count, seg_room = _segment_tokens(agent, text[start:end], questions)
            if count <= seg_room:
                chosen = (end, count)
                break
        if chosen is None:
            raise _PlanError(f"{start}번째 문자부터 token budget에 맞는 조각을 찾지 못함", plan)
        plan["segments"].append({"start_char": start, "end_char": chosen[0],
                                 "input_tokens": chosen[1]})
        start = chosen[0]
    return plan


def _plan_texts(agent, texts, questions, max_chars):
    """모델 추론 없이 tokenizer로만 원문별 분할 계획을 만든다. 실패 항목은 error로 남긴다."""
    results = []
    for text in texts:
        try:
            results.append(_plan_one(agent, text, questions, max_chars))
        except _PlanError as exc:
            results.append({"input_tokens": exc.plan["input_tokens"],
                            "token_limit": exc.plan["token_limit"], "segments": [],
                            "error": str(exc)})
        except Exception as exc:
            results.append({"input_tokens": None, "token_limit": None, "segments": [],
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


def _valid_plans(plans, texts, max_chars):
    if not isinstance(plans, list) or len(plans) != len(texts):
        return False
    for text, plan in zip(texts, plans):
        if not isinstance(plan, dict) or not isinstance(plan.get("segments"), list):
            return False
        if "error" in plan:
            if not isinstance(plan["error"], str) or plan["segments"]:
                return False
            continue
        position = 0
        for seg in plan["segments"]:
            if not isinstance(seg, dict) or seg.get("start_char") != position:
                return False
            end = seg.get("end_char")
            if not isinstance(end, int) or isinstance(end, bool) or end - position > max_chars:
                return False
            if end <= position and text:
                return False
            position = end
        if position != len(text) or (not plan["segments"] and text):
            return False
    return True


def _model_worker(conn, model_path):
    from laya.agent import Agent

    agent = Agent(model_path, device="cpu")
    _send_message(conn, {"status": "ready"})
    while True:
        request = _receive_message(conn)
        op = request.get("op")
        if op == "plan":
            plans = _plan_texts(agent, request["texts"], request["questions"],
                                request["max_chars"])
            _send_message(conn, {"status": "ok", "plans": plans})
            continue
        if op != "predict":
            raise RuntimeError("알 수 없는 Laya 요청")
        outputs = _checked_predict(agent, request["texts"], request["questions"],
                                   request["batch_size"])
        _send_message(conn, {"status": "ok", "outputs": outputs})


def _child_entry(conn, model_path, target):
    # Python 출력뿐 아니라 native 라이브러리 로그도 UI stdout 프로토콜과 격리한다.
    with open(os.devnull, "w", encoding="utf-8") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        sys.stdout = sys.stderr = sink
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        try:
            (target or _model_worker)(conn, model_path)
        except EOFError:
            pass
        except BaseException as exc:
            try:
                _send_message(conn, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})
            except BaseException:
                pass  # 부모가 취소하며 Pipe를 닫았으면 오류 응답도 보낼 수 없다.
        finally:
            conn.close()


class LayaRunner:
    """한 scan의 Laya 단계에서 한 번 사용하는 동기 context manager.

    deadline은 time.perf_counter() 절대시각, cancel은 is_set()을 제공한다.
    target(conn, model_path)은 spawn으로 import 가능한 테스트용 최상위 함수다.
    Pipe에는 UTF-8 JSON으로 ready/ok/error 응답과 predict 요청을 주고받는다.
    호출은 직렬이며, context 종료 후 pid는 정리 증거용으로 유지한다.
    """

    def __init__(self, model_path, *, deadline=None, cancel=None, target=None):
        self.model_path = os.fspath(model_path)
        self.deadline = deadline
        self.cancel = cancel
        self._target = target
        self._process = None
        self._conn = None
        self._io_thread = None
        self._pid = None
        self._used = False
        self._cleanup_error = None

    @property
    def pid(self):
        return self._pid

    def _check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise asyncio.CancelledError("Laya 점검 취소")
        if self.deadline is not None and time.perf_counter() >= self.deadline:
            raise TimeoutError("Laya 공유 점검 시간 예산 초과")

    def __enter__(self):
        if self._used:
            raise RuntimeError("LayaRunner는 한 scan에서 한 번만 열 수 있음")
        self._check()
        self._used = True
        ctx = multiprocessing.get_context("spawn")
        try:
            self._conn, child_conn = ctx.Pipe(duplex=True)
            try:
                self._process = ctx.Process(target=_child_entry,
                                            args=(child_conn, self.model_path, self._target),
                                            name="laya-worker", daemon=True)
                self._process.start()
                self._pid = self._process.pid
            finally:
                # 부모가 반대쪽 끝을 보유하면 child kill 뒤에도 recv가 EOF를 못 받는다.
                child_conn.close()
            response = self._exchange()
            if response.get("status") != "ready":
                raise RuntimeError("Laya 로드 완료 응답 누락")
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, (asyncio.CancelledError, TimeoutError, RuntimeError,
                                KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError(f"Laya 시작 실패: {exc}") from exc

    def _exchange(self, request=None):
        if self._conn is None or self._process is None:
            raise RuntimeError("열려 있는 LayaRunner context가 필요함")
        if self._io_thread is not None:
            raise RuntimeError("LayaRunner 호출은 직렬이어야 함")
        conn = self._conn
        done = threading.Event()
        result = {}

        def exchange():
            try:
                if request is not None:
                    _send_message(conn, request)
                result["response"] = _receive_message(conn)
            except BaseException as exc:
                result["error"] = exc
            finally:
                done.set()

        try:
            self._check()
            # send/recv는 Pipe 버퍼나 부분 프레임에서 블록될 수 있다. 부모는 감시만 한다.
            self._io_thread = threading.Thread(target=exchange, name="laya-io", daemon=True)
            self._io_thread.start()
            while not done.wait(_POLL_SECONDS):
                self._check()
                if not self._process.is_alive():
                    raise RuntimeError("Laya 자식 프로세스가 응답 없이 종료됨")
            self._check()
            self._io_thread.join(_JOIN_SECONDS)
            if self._io_thread.is_alive():
                raise RuntimeError("Laya 통신 스레드가 종료되지 않음")
            self._io_thread = None
            if "error" in result:
                raise RuntimeError(f"Laya IPC 실패: {result['error']}") from result["error"]
            response = result["response"]
            if response.get("status") == "error":
                raise RuntimeError(f"Laya 실행 실패: {response.get('error', '오류 내용 없음')}")
            return response
        except BaseException:
            self.close()
            raise

    def predict_batch(self, texts, questions, batch_size=16):
        response = self._exchange({"op": "predict", "texts": texts,
                                   "questions": questions, "batch_size": batch_size})
        if response.get("status") != "ok" or not isinstance(response.get("outputs"), list):
            self.close()
            raise RuntimeError("잘못된 Laya 예측 응답")
        # answers와 usage를 포함한 원본 출력은 호출부의 검증을 위해 그대로 유지한다.
        return response["outputs"]

    def plan_batch(self, texts, questions, *, max_chars=1024):
        """모델 추론 없이 각 원문을 token budget에 맞는 char 범위 조각으로 계획한다."""
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise ValueError("max_chars는 양의 정수여야 함")
        if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
            raise ValueError("texts는 문자열 목록이어야 함")
        response = self._exchange({"op": "plan", "texts": texts, "questions": questions,
                                   "max_chars": max_chars})
        plans = response.get("plans")
        if response.get("status") != "ok" or not _valid_plans(plans, texts, max_chars):
            self.close()
            raise RuntimeError("잘못된 Laya 계획 응답")
        return plans

    def close(self):
        if self._cleanup_error is not None:
            # _exchange, __enter__, __exit__의 중첩 정리가 회수 예산을 반복하지 않는다.
            raise self._cleanup_error
        process = self._process
        thread = self._io_thread
        errors = []
        try:
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(_JOIN_SECONDS)
        except Exception as exc:
            errors.append(f"자식 terminate/join 실패: {exc}")
        try:
            if process is not None and process.is_alive():
                process.kill()
                process.join(_JOIN_SECONDS)
        except Exception as exc:
            errors.append(f"자식 kill/join 실패: {exc}")
        # 자식의 반대쪽 끝이 닫히면 막힌 송수신도 EOF/BrokenPipe로 끝난다.
        try:
            if thread is not None and thread.ident is not None:
                thread.join(_JOIN_SECONDS)
        except Exception as exc:
            errors.append(f"통신 스레드 join 실패: {exc}")
        try:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        except Exception as exc:
            errors.append(f"Pipe close 실패: {exc}")
        try:
            if thread is not None and thread.is_alive():
                errors.append("Laya 통신 스레드 회수 실패")
            else:
                self._io_thread = None
            if process is not None:
                if process.is_alive():
                    errors.append("Laya 자식 프로세스 회수 실패")
                else:
                    process.close()
                    self._process = None
        except Exception as exc:
            errors.append(f"자식/스레드 종료 확인 또는 핸들 close 실패: {exc}")
        if errors:
            self._cleanup_error = LayaCleanupError(f"Laya 정리 실패(pid={self.pid}): " + "; ".join(errors))
            raise self._cleanup_error

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
