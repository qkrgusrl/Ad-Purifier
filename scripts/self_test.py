"""동봉 자산의 실제 로드/추론/브라우저/I/O 검사. 정확도 평가가 아니다."""
import gc
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app_paths import app_root, gemma_model, laya_model, resource_root


def verify_child_runtime_modules(pid, runtime_dir):
    """실행 중인 llama 자식의 실제 DLL 로드 경로를 검사한다."""
    import psutil

    if type(pid) is not int or pid <= 0:
        raise ValueError('실제 자식 PID 필요')
    runtime = Path(runtime_dir).resolve()
    process = psutil.Process(pid)
    executable = Path(process.exe()).resolve()
    expected_executable = runtime / 'llama-server.exe'
    if str(executable).casefold() != str(expected_executable).casefold():
        raise RuntimeError(f'Gemma 자식 실행 경로 불일치: {executable}')
    modules = sorted({str(Path(item.path).resolve()) for item in process.memory_maps()})
    required = {'llama-server-impl.dll', 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'}
    redistributable = {
        'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
        'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll',
        'vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_threads.dll',
        'concrt140.dll', 'vccorlib140.dll',
    }
    found = set()
    for module in modules:
        path = Path(module)
        name = path.name.casefold()
        if name in required or name in redistributable:
            if str(path.parent).casefold() != str(runtime).casefold():
                raise RuntimeError(f'앱 로컬 DLL 외부 로드: {module}')
            found.add(name)
    missing = required - found
    if missing:
        raise RuntimeError('Gemma 필수 DLL 로드 누락: ' + ', '.join(sorted(missing)))
    return {'pid': pid, 'executable': str(executable), 'runtime_dir': str(runtime),
            'required_modules': sorted(required), 'loaded_modules': modules, 'app_local_verified': True}


def run_self_test(output):
    output = Path(output).resolve()
    result = {'status': 'running', 'accuracy_guarantee': False, 'frozen': bool(getattr(sys, 'frozen', False)),
              'app_root': str(app_root()), 'resource_root': str(resource_root()), 'cwd': str(Path.cwd()),
              'python_executable': sys.executable, 'phases': {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    def phase(name, action):
        started = time.perf_counter()
        result['phases'][name] = {'status': 'running'}
        save()
        try:
            raw = action()
            result['phases'][name].update(status='ok', raw=raw)
        except BaseException as exc:
            result['phases'][name].update(status='failed', error=f'{type(exc).__name__}: {exc}',
                                          traceback=traceback.format_exc())
        result['phases'][name]['seconds'] = time.perf_counter() - started
        save()
        gc.collect()
    save()

    def assets():
        from scripts.assets import verify_assets
        manifest = json.loads((app_root() / 'installed-assets.json').read_text(encoding='utf-8'))
        verify_assets(app_root(), manifest)
        return {'verified_files': len(manifest['files']), 'manifest': 'installed-assets.json'}
    phase('asset_integrity', assets)
    if result['phases']['asset_integrity']['status'] != 'ok':
        result['status'] = 'failed'; save(); return 1

    def browser_fixture():
        from playwright.sync_api import sync_playwright
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                body = '<!doctype html><html><title>설치 시험</title><body><p id="fixture">설치 경로 한글 I/O 확인</p></body></html>'.encode()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(f'http://127.0.0.1:{server.server_port}/', timeout=30000)
                    text = page.locator('#fixture').inner_text()
                    if text != '설치 경로 한글 I/O 확인':
                        raise ValueError('local fixture 내용 불일치')
                    extracted = page.evaluate((resource_root() / 'detector/extract.js').read_text(encoding='utf-8'))
                    raw = {'title': page.title(), 'text': text, 'extract': extracted, 'browser_version': browser.version,
                           'browser_path': playwright.chromium.executable_path,
                           'PLAYWRIGHT_BROWSERS_PATH': os.environ['PLAYWRIGHT_BROWSERS_PATH']}
                    # 실제 결과 기본 위치인 exe 폴더에 생성하고 읽는다.
                    with tempfile.TemporaryDirectory(prefix='self-test-한글-', dir=app_root()) as temp:
                        probe = Path(temp) / '결과.json'
                        probe.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
                        if json.loads(probe.read_text(encoding='utf-8')) != raw:
                            raise ValueError('I/O roundtrip 불일치')
                    return raw
                finally:
                    browser.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)
    phase('chromium_fixture_io', browser_fixture)

    def laya():
        import multiprocessing
        from importlib.metadata import version
        import psutil
        from detector.laya_runtime import LayaRunner
        from training.make_dataset import QUESTION

        torch_version = version('torch')
        if sys.platform == 'win32' and not torch_version.endswith('+cpu'):
            raise RuntimeError('CPU 전용 torch wheel이 아님')
        runner = LayaRunner(str(laya_model()), deadline=time.perf_counter() + 600, cancel=threading.Event())
        raw = {'model': str(laya_model()), 'torch': torch_version, 'device': 'cpu',
               'runtime': 'detector.laya_runtime.LayaRunner', 'start_method': 'spawn'}
        result['phases']['laya_load_infer']['raw'] = raw
        save()
        try:
            with runner:
                raw['pid'] = runner.pid
                if type(runner.pid) is not int or runner.pid == os.getpid() or not psutil.pid_exists(runner.pid):
                    raise RuntimeError('실제 Laya 자식 프로세스 시작 증거 없음')
                prediction = runner.predict_batch(['시립 도서관 운영 시간 안내입니다.'], QUESTION, batch_size=1)
                raw['prediction'] = prediction
                save()
                json.dumps(prediction, ensure_ascii=False, allow_nan=False)
                if len(prediction) != 1 or 'answers' not in prediction[0]:
                    raise ValueError('Laya 추론 envelope 오류')
        finally:
            pid = runner.pid
            raw['pid'] = pid
            raw['cleanup'] = {'active_child': pid is not None and any(p.pid == pid for p in multiprocessing.active_children()),
                              'process_running': pid is not None and psutil.pid_exists(pid)}
            save()
            if raw['cleanup']['active_child'] or raw['cleanup']['process_running']:
                raise RuntimeError('Laya 소유 자식 프로세스 정리 실패')
        return raw
    phase('laya_load_infer', laya)
    if result['phases']['laya_load_infer']['status'] != 'ok':
        result['phases']['gemma_load_infer'] = {'status': 'not_run', 'reason': 'Laya 로드/추론/정리 검사 실패'}
        result['status'] = 'failed'
        save()
        return 1

    def gemma():
        from local_llm.client import GemmaClient
        with GemmaClient(deadline=time.perf_counter() + 600) as client:
            owned = client.server
            verify = client.verify({'candidate_id': 'installation-fixture', 'html': '<p>시립 도서관 운영 시간 안내입니다.</p>', 'context': ''})
            extract = client.extract({'blocks': [{'block_id': 'installation-block', 'html': '<p>시립 도서관 운영 시간 안내입니다.</p>', 'context': ''}]})
            raw = {'model': str(gemma_model()), 'verify': verify, 'extract': extract, 'runtime': client.server.provenance()}
            result['phases']['gemma_load_infer']['raw'] = raw
            save()
            # 생성이 끝나도 context 안에서는 자식이 살아 있다. Windows 실제 로드 모듈을 검사한다.
            if sys.platform == 'win32':
                raw['module_audit'] = verify_child_runtime_modules(owned.proc.pid, Path(owned.paths['server']).parent)
            # uncertain은 정확도 문제가 아니라 유효한 프로토콜 결과다. 호출 자체 실패는 통과시키지 않는다.
            result['phases']['gemma_load_infer']['raw'] = raw
            save()
            calls = [r['call'] for r in verify['results']] + [bc for c in extract['calls'] for bc in c.get('block_calls', [c])]
            if len(calls) < 2 or any(c['status'] != 'ok' or c.get('request_count', 0) != 1 for c in calls):
                raise ValueError('Gemma 실제 생성/원문 결합 검사 실패')
        raw['cleanup'] = {'process_running': owned.proc.poll() is None, 'key_removed': not owned.key_file.exists()}
        if raw['cleanup']['process_running'] or not raw['cleanup']['key_removed']:
            raise RuntimeError('Gemma 소유 프로세스/세션 키 정리 실패')
        return raw
    phase('gemma_load_infer', gemma)
    result['status'] = 'ok' if all(result['phases'][key]['status'] == 'ok' for key in
                                 ('asset_integrity', 'chromium_fixture_io', 'laya_load_infer', 'gemma_load_infer')) else 'failed'
    save()
    return 0 if result['status'] == 'ok' else 1
