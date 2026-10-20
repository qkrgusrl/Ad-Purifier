"""CPU llama-server 한 개를 소유하며 v15 입력과 근거 ID 계약을 지킨다."""
import asyncio
import hashlib
import json
import os
import sys
from collections import OrderedDict
from contextlib import contextmanager
import platform
import queue
import secrets
import socket
import uuid
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
from app_paths import data_root, gemma_model, llama_server
from .prompts import (DEFAULT_VERSION, PROMPTS, SENTENCE_ID_VERSIONS,
                      input_only, prepare_input, source_units, schema_for, verification_token_limit)


_dll_launch_lock = threading.Lock()
_semantic_cache_lock = threading.Lock()
SEMANTIC_CACHE_LIMIT = 256


def _positive_option(value, name, default=None):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise ValueError(f'{name}: 0보다 큰 정수가 필요합니다') from None
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name}: 0보다 큰 정수가 필요합니다')
    return value


@contextmanager
def external_server_environment():
    """PyInstaller DLL 검색 경로가 독립 llama CPU 배포물로 전파되지 않게 한다."""
    env = os.environ.copy()
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        yield env
        return
    import ctypes
    from ctypes import wintypes
    bundle = Path(sys._MEIPASS).resolve()
    env['PATH'] = os.pathsep.join(part for part in env.get('PATH', '').split(os.pathsep)
                                 if part and not Path(part).resolve().is_relative_to(bundle))
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_directory = kernel.GetDllDirectoryW
    get_directory.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    get_directory.restype = wintypes.DWORD
    set_directory = kernel.SetDllDirectoryW
    set_directory.argtypes = [wintypes.LPCWSTR]
    set_directory.restype = wintypes.BOOL
    # 프로세스 전역 API이므로 이 앱의 동시 llama launch를 직렬화한다.
    with _dll_launch_lock:
        buffer = ctypes.create_unicode_buffer(get_directory(0, None) + 1)
        get_directory(len(buffer), buffer)
        previous = buffer.value
        if not set_directory(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield env
        finally:
            if not set_directory(previous or None):
                raise ctypes.WinError(ctypes.get_last_error())


class Budget:
    """deadline은 time.perf_counter() 기준 절대시각이다."""
    def __init__(self, deadline=None, cancel=None):
        self.deadline = deadline if deadline is not None else time.perf_counter() + 1800
        self.cancel = cancel

    def remaining(self):
        if self.cancel is not None and self.cancel.is_set():
            raise asyncio.CancelledError('점검 취소')
        left = self.deadline - time.perf_counter()
        if left <= 0:
            raise TimeoutError('Gemma 공용 시간 예산 소진')
        return left


def http_json(base, path, payload=None, timeout=300, api_key=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    request = urllib.request.Request(base + path, data=body, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('서버 응답 크기 초과')
        return json.loads(raw)


class LocalServer:
    def __init__(self, log_dir=None, threads=None, ctx_size=4096, repack=True,
                 cache_prompt=None, deadline=None, cancel=None, budget=None, on_event=None,
                 *, threads_batch=None, ubatch_size=None):
        self.on_event = on_event
        self.paths = {'server': str(llama_server()), 'model': str(gemma_model())}
        self.threads = _positive_option(threads if threads is not None else os.environ.get('AD_PURIFIER_GEMMA_THREADS'), 'Gemma threads')
        self.threads_batch = _positive_option(
            threads_batch if threads_batch is not None else os.environ.get('AD_PURIFIER_GEMMA_THREADS_BATCH'),
            'Gemma threads_batch', self.threads)
        self.ubatch_size = _positive_option(
            ubatch_size if ubatch_size is not None else os.environ.get('AD_PURIFIER_GEMMA_UBATCH_SIZE'),
            'Gemma ubatch_size', 128)
        if self.ubatch_size > 512:
            raise ValueError('Gemma ubatch_size는 batch_size 512 이하여야 합니다')
        self.ctx_size = ctx_size
        self.repack, self.cache_prompt = repack, cache_prompt
        self.budget = budget or Budget(deadline, cancel)
        self.log_dir = Path(log_dir) if log_dir else Path(tempfile.mkdtemp(prefix='gemma-', dir=data_root()))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.stop_event = threading.Event()
        self.proc = None
        self.peak_rss = 0
        self._semantic_cache_data = OrderedDict()

    def __enter__(self):
        self.budget.remaining()
        for path in self.paths.values():
            if not Path(path).is_file():
                raise FileNotFoundError(path)
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            self.base = f'http://127.0.0.1:{port}'
            self.api_key = secrets.token_urlsafe(32)
            # 고유 파일 + 생성 순간부터 사용자 전용 권한. 설치 폴더에 비밀키를 쓰지 않는다.
            fd, name = tempfile.mkstemp(prefix='.session-', dir=self.log_dir)
            self.key_file = Path(name)
            with os.fdopen(fd, 'w') as stream:
                stream.write(self.api_key)
            self.command = [self.paths['server'], '-m', self.paths['model'], '--host', '127.0.0.1', '--port', str(port),
                            '--device', 'none', '--gpu-layers', '0', '--no-op-offload', '--no-kv-offload']
            if self.threads is not None:
                self.command.extend(['--threads', str(self.threads)])
            if self.threads_batch is not None:
                self.command.extend(['--threads-batch', str(self.threads_batch)])
            self.command.extend([
                            '--ctx-size', str(self.ctx_size), '--parallel', '1', '--batch-size', '512', '--ubatch-size', str(self.ubatch_size),
                            '--fit', 'off', '--no-context-shift', '--no-webui', '--offline', '--verbosity', '4',
                            '--api-key-file', str(self.key_file)])
            if not self.repack:
                self.command.append('--no-repack')
            self.log = (self.log_dir / 'server.log').open('w', encoding='utf-8')
            started = time.perf_counter()
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            with external_server_environment() as env:
                self.proc = subprocess.Popen(self.command, stdout=self.log, stderr=subprocess.STDOUT,
                                             creationflags=flags, env=env, cwd=Path(self.paths['server']).parent)
            self.monitor = threading.Thread(target=self._monitor, daemon=True)
            self.monitor.start()
            startup_end = min(self.budget.deadline, started + 180)
            while time.perf_counter() < startup_end:
                self.budget.remaining()
                if self.proc.poll() is not None:
                    raise RuntimeError(f'서버 종료: {self.log_dir / "server.log"}')
                try:
                    if self.request('/health', timeout=min(2, startup_end-time.perf_counter())).get('status') == 'ok':
                        self.startup_seconds = time.perf_counter() - started
                        return self
                except (OSError, urllib.error.URLError, ValueError):
                    pass
                self.stop_event.wait(max(0, min(.05, self.budget.remaining(), startup_end-time.perf_counter())))
            raise TimeoutError('서버 준비 시간 초과')
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _monitor(self):
        try:
            process = psutil.Process(self.proc.pid)
            while not self.stop_event.wait(.1):
                rss = sum(p.memory_info().rss for p in [process, *process.children(recursive=True)] if p.is_running())
                self.peak_rss = max(self.peak_rss, rss)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def __exit__(self, *_):
        self.stop_event.set()
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=10)
        finally:
            try:
                if hasattr(self, 'monitor'):
                    self.monitor.join(timeout=2)
            finally:
                try:
                    if hasattr(self, 'log'):
                        self.log.close()
                finally:
                    if hasattr(self, 'key_file'):
                        self.key_file.unlink(missing_ok=True)
                    self.api_key = None

    def request(self, path, payload=None, timeout=300, on_dispatch=None):
        # socket timeout은 개별 read마다 갱신될 수 있으므로 전체 요청도 절대시각으로 제한한다.
        allowed = min(timeout, self.budget.remaining())
        if allowed <= 0:
            raise TimeoutError('HTTP 요청 시간 초과')
        expires = time.perf_counter() + allowed
        completed = queue.Queue(maxsize=1)
        base, api_key = self.base, self.api_key
        def fetch():
            try:
                completed.put((True, http_json(base, path, payload, allowed, api_key)))
            except BaseException as exc:
                completed.put((False, exc))
        if on_dispatch is not None:
            on_dispatch()
        threading.Thread(target=fetch, daemon=True).start()
        while True:
            wait = min(.05, self.budget.remaining(), expires-time.perf_counter())
            if wait <= 0:
                raise TimeoutError('HTTP 요청 시간 초과')
            try:
                ok, value = completed.get(timeout=wait)
            except queue.Empty:
                continue
            self.budget.remaining()
            if ok:
                return value
            raise value

    def provenance(self):
        return {'platform': platform.platform(), 'threads': self.threads, 'threads_batch': self.threads_batch,
                'batch_size': 512, 'ubatch_size': self.ubatch_size, 'context_tokens': self.ctx_size,
                'backend': 'CPU', 'gpu_layers': 0, 'startup_seconds': self.startup_seconds,
                'peak_server_rss_bytes': self.peak_rss, **self.paths}

    def prompt_size(self, task, data, version):
        clean_input = prepare_input(task, data, version)
        messages = [{"role": "system", "content": PROMPTS[version][task]},
                    {"role": "user", "content": json.dumps(clean_input, ensure_ascii=False)}]
        template = self.request("/apply-template", {"messages": messages, "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"})
        return len(self.request("/tokenize", {"content": template["prompt"], "add_special": False, "parse_special": True})["tokens"])

    def _emit_event(self, event):
        observer = getattr(self, "on_event", None)
        if observer is not None:
            try:
                observer(dict(event))
            except Exception:
                # 관측 실패는 추론 결과나 모델 실행 정책을 바꾸지 않는다.
                try:
                    observer({"event": "audit_error"})
                except Exception:
                    pass

    def _semantic_cache_key(self, system, request, clean_input, version):
        paths = getattr(self, "paths", None)
        model = paths.get("model") if isinstance(paths, dict) else None
        if not isinstance(model, str) or not model:
            return None  # 모델 식별이 없으면 결과 공유 금지
        try:
            stat = os.stat(model)
            identity = [model, stat.st_size, stat.st_mtime_ns]
        except OSError:
            identity = [model, None, None]
        payload = {"cache_schema": 1, "version": version, "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
                   "model": identity, "ctx_size": getattr(self, "ctx_size", None), "request": request,
                   "raw_source_sha256": source_digest(clean_input)}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _semantic_cache_get(self, key):
        with _semantic_cache_lock:
            cache = getattr(self, "_semantic_cache_data", None)
            if cache is None:
                return None
            entry = cache.get(key)
            if entry is not None:
                cache.move_to_end(key)
            return dict(entry) if entry is not None else None

    def _semantic_cache_evict(self, key):
        with _semantic_cache_lock:
            cache = getattr(self, "_semantic_cache_data", None)
            if cache is not None:
                cache.pop(key, None)

    def _semantic_cache_put(self, key, choice):
        if not (isinstance(choice, dict) and set(choice) == {"verdict", "evidence_id"}):
            return
        entry = {"verdict": choice["verdict"], "evidence_id": choice["evidence_id"]}
        with _semantic_cache_lock:
            cache = getattr(self, "_semantic_cache_data", None)
            if cache is None:
                cache = OrderedDict()
                try:
                    self._semantic_cache_data = cache
                except Exception:
                    return
            cache[key] = entry
            cache.move_to_end(key)
            while len(cache) > SEMANTIC_CACHE_LIMIT:
                cache.popitem(last=False)

    def infer(self, task, data, version=DEFAULT_VERSION, max_tokens=None):
        if version != DEFAULT_VERSION:
            raise ValueError("v15만 지원")
        if task == "extract":
            return self._isolated_extract(data, version)
        started = time.perf_counter()
        clean_input = input_only(task, data)
        system = PROMPTS[version][task]
        limit = max_tokens or (verification_token_limit(version) if task == "verify" else 512)
        cache_setting = getattr(self, "cache_prompt", None)
        use_cache = version in SENTENCE_ID_VERSIONS if cache_setting is None else cache_setting
        result = {"task": task, "version": version, "input": clean_input,
                  "prompt_sha256": hashlib.sha256(system.encode()).hexdigest(), "max_tokens": limit,
                  "temperature": 0, "seed": 42, "thinking": False, "cache_prompt": use_cache, "request_count":0}
        request_id = uuid.uuid4().hex
        overflow_preflight = False
        event_extra = {}
        def emit(name):
            self._emit_event({"event": name, "request_id": request_id,
                              "candidate_id": clean_input.get("candidate_id"), "task": task,
                              "status": result.get("status", "started"),
                              "completion_received": bool(result.get("completion_received", False)),
                              "request_count": result["request_count"],
                              "preflight_failed": int(bool(result.get("preflight_failed") or overflow_preflight or
                                  result.get("decision_rule") == "empty_source_needs_review")), **event_extra})
        try:
            model_input=prepare_input(task,clean_input,version)
            if version in SENTENCE_ID_VERSIONS:
                result['model_input']=model_input
                if not any(u['selectable'] for u in model_input['source_units']):
                    result.update({'status':'ok','parsed':{'verdict':'uncertain','evidence':None,'evidence_id':None,
                                                         'source_field':None,'start_char':None,'end_char':None,
                                                         'source_sha256':source_digest(clean_input),'evidence_scope':'no_source'},
                                   'validation':{'errors':[],'accepted':[],'rejected_count':0},
                                   'decision_rule':'empty_source_needs_review','request_count':0,'inference_seconds':0,
                                   'wall_seconds':time.perf_counter()-started})
                    return result
            messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(model_input, ensure_ascii=False)}]
            request = {"messages": messages, "temperature": 0, "seed": 42, "max_tokens": limit,
                       "cache_prompt": use_cache, "chat_template_kwargs": {"enable_thinking": False},
                       "reasoning_effort": "none", "response_format": {"type": "json_object", "schema": schema_for(task, model_input, version)}}
            cache_key = (self._semantic_cache_key(system, request, clean_input, version)
                         if task == "verify" and version in SENTENCE_ID_VERSIONS else None)
            budget = getattr(self, "budget", None)
            if cache_key:
                result["cache_key"] = cache_key
                event_extra["cache_key"] = cache_key
                if budget is not None:
                    budget.remaining()  # 캐시 경로도 취소/기한을 우회하지 않는다.
                entry = self._semantic_cache_get(cache_key)
                if entry is not None:
                    try:
                        choice = {"verdict": entry.get("verdict"), "evidence_id": entry.get("evidence_id")}
                        if set(entry) != {"verdict", "evidence_id"}:
                            raise ValueError("cache_entry_schema")
                        parsed = resolve_sentence_choice(clean_input, choice, version)
                        checked = {key: parsed[key] for key in ("verdict", "evidence")}
                        validation = validate_output(task, clean_input, checked)
                        if validation["errors"]:
                            raise ValueError("cache_binding_invalid")
                    except Exception:
                        self._semantic_cache_evict(cache_key)  # 결합 실패: 폐기 후 새 추론
                    else:
                        if budget is not None:
                            budget.remaining()
                        event_extra.update({"status": "reused", "cache_hit": True, "cache_key": cache_key,
                                            "reuse_kind": "semantic_cache"})
                        result.update({"model_output": choice, "parsed": parsed, "validation": validation,
                                       "status": "ok", "request_count": 0, "inference_seconds": 0,
                                       "cache_hit": True, "cache_key": cache_key, "reuse_kind": "semantic_cache",
                                       "wall_seconds": time.perf_counter() - started})
                        return result
            try:
                prompt_tokens = self.prompt_size(task, clean_input, version)
            except Exception:
                result['preflight_failed'] = 1
                raise
            result["preflight_prompt_tokens"] = prompt_tokens
            if prompt_tokens + limit + 64 > self.ctx_size:
                overflow_preflight = True
                raise ValueError(f"input_overflow: 원문을 자르지 않고 입력을 분할해야 함 ({prompt_tokens}+{limit}+64 > {self.ctx_size}토큰)")
            inference_start = time.perf_counter()
            def on_dispatch():
                result['request_count'] = 1
                emit('request_start')
            response = self.request("/v1/chat/completions", request, on_dispatch=on_dispatch)
            result['completion_received'] = True
            result["inference_seconds"] = time.perf_counter() - inference_start
            result["raw_response"] = response
            choice = response["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ValueError("generation_incomplete:" + str(choice["finish_reason"]))
            parsed = json.loads(choice["message"]["content"])
            if task=='verify' and version in SENTENCE_ID_VERSIONS:
                result['model_output']=parsed
                parsed=resolve_sentence_choice(clean_input,parsed,version)
            result["parsed"] = parsed
            checked = {key: parsed[key] for key in ("verdict", "evidence")} if task == "verify" and version in SENTENCE_ID_VERSIONS else parsed
            result["validation"] = validate_output(task, clean_input, checked)
            result["status"] = "ok" if not result["validation"]["errors"] else "invalid_output"
            if cache_key and result["status"] == "ok":
                self._semantic_cache_put(cache_key, result.get("model_output"))
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if "status" not in result:
                result["status"] = "cancelled"
            if result["request_count"]:
                emit("request_end")
            emit("item_end")
        result["wall_seconds"] = time.perf_counter() - started
        return result

    def _isolated_extract(self, data, version):
        started = time.perf_counter()
        clean_input = input_only("extract", data)
        calls, candidates, unresolved, failed = [], [], [], []
        bindings={}
        for block in clean_input["blocks"]:
            call = self.infer("verify", {"candidate_id": block["block_id"], "html": block["html"], "context": block["context"]}, version)
            calls.append(call)
            if call["status"] != "ok":
                failed.append(block["block_id"])
                continue
            verdict = call["parsed"]["verdict"]
            if verdict == "ad":
                candidates.append({"block_id": block["block_id"], "evidence": call["parsed"]["evidence"]})
                if 'evidence_id' in call['parsed']:
                    bindings[block['block_id']]=call['parsed']
            elif verdict == "uncertain":
                unresolved.append(block["block_id"])
        parsed = {"candidates": candidates}
        validation = validate_output("extract", clean_input, parsed)
        return {"task": "extract", "version": version, "input": clean_input,
                "prompt_sha256": hashlib.sha256(json.dumps(PROMPTS[version], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "temperature": 0, "seed": 42, "thinking": False,
                "cache_prompt": calls[0]["cache_prompt"] if calls and "cache_prompt" in calls[0] else False,
                "status": "invalid_output" if failed or validation["errors"] else "ok",
                "parsed": parsed, "validation": validation, "unresolved_block_ids": unresolved,
                "failed_block_ids": failed, "block_calls": calls, "request_count": sum(c.get('request_count',1) for c in calls),
                "source_bindings":bindings,
                "inference_seconds": sum(c.get("inference_seconds", 0) for c in calls),
                "wall_seconds": time.perf_counter() - started}

def _grounded(quote, data):
    return isinstance(quote, str) and bool(quote.strip()) and any(quote in data.get(key, "") for key in ("html", "context"))

def source_digest(data):
    return hashlib.sha256(json.dumps({k:data.get(k,'') for k in ('html','context')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def resolve_sentence_choice(data,obj,version=None):
    original_verdict=obj.get('verdict') if isinstance(obj,dict) else None
    if not isinstance(obj,dict) or set(obj)!={'verdict','evidence_id'} or obj['verdict'] not in ('ad','clean','uncertain'):
        raise ValueError('sentence_choice_schema: verdict/evidence_id 필요')
    if isinstance(obj['evidence_id'], str) and obj['evidence_id'].startswith('c'):
        if obj['verdict'] == 'clean':
            raise ValueError('clean_requires_target_html: context만으로 대상 정상 확정 불가')
        raise ValueError('unbound_sentence_id: context ID는 선택할 수 없음')
    chosen=next((u for u in source_units(data) if u['id']==obj['evidence_id'] and u['selectable']),None)
    if chosen is None or (chosen.get('structural_only') and obj['verdict']!='uncertain'):
        raise ValueError('unbound_sentence_id: 선택할 수 없는 ID 또는 내용 없는 확정')
    field,start,end=chosen['source_field'],chosen['start_char'],chosen['end_char']
    if obj['verdict'] == 'clean' and field != 'html':
        raise ValueError('clean_requires_target_html: context만으로 대상 정상 확정 불가')
    return {'verdict':obj['verdict'],'model_verdict':original_verdict,'evidence':data.get(field,'')[start:end],
            'evidence_id':chosen['id'],'source_field':field,'start_char':start,'end_char':end,
            'source_sha256':source_digest(data),'owner_id':data['candidate_id'],'evidence_scope':'source_unit_selection'}

def validate_output(task, data, obj):
    errors, accepted = [], []
    if not isinstance(obj, dict):
        return {"errors": ["not_object"], "accepted": [], "rejected_count": 1}
    if task == "verify":
        if set(obj) != {"verdict", "evidence"} or obj.get("verdict") not in ("ad", "clean", "uncertain"):
            errors.append("invalid_schema")
        if not _grounded(obj.get("evidence"), data):
            errors.append("unbound_evidence")
        return {"errors": errors, "accepted": [], "rejected_count": int(bool(errors))}
    if set(obj) != {"candidates"} or not isinstance(obj.get("candidates"), list):
        return {"errors": ["invalid_schema"], "accepted": [], "rejected_count": 1}
    sources = {b["block_id"]: b for b in data["blocks"]}
    seen, rejected = set(), 0
    for item in obj["candidates"]:
        ident = item.get("block_id") if isinstance(item, dict) else None
        issue = None
        if not isinstance(item, dict) or set(item) != {"block_id", "evidence"} or not isinstance(ident, str) or ident not in sources:
            issue = "unknown_block_or_schema"
        elif ident in seen:
            issue = "duplicate_block"
        elif not _grounded(item.get("evidence"), sources[ident]):
            issue = "unbound_evidence"
        if isinstance(ident, str):
            seen.add(ident)
        if issue:
            errors.append(f"{issue}:{ident}")
            rejected += 1
        else:
            accepted.append(item)
    return {"errors": errors, "accepted": accepted, "rejected_count": rejected}
