"""실행 폴더의 자산과 사용자 쓰기 가능 로그 경로."""
import os
import sys
import tempfile
from pathlib import Path


def app_root():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def resource_root():
    return Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else app_root()


def _asset(variable, relative):
    path = Path(os.environ.get(variable) or relative)
    return path if path.is_absolute() else app_root() / path


def laya_model():
    return _asset('AD_PURIFIER_MODEL', 'models/laya-illegal-ad-long2')


def gemma_model():
    return _asset('AD_PURIFIER_GEMMA_MODEL', 'models/gemma/gemma-4-E2B_q4_0-it.gguf')


def llama_server():
    return _asset('AD_PURIFIER_LLAMA_SERVER', 'runtime/llama/llama-server' + ('.exe' if sys.platform == 'win32' else ''))


def data_root():
    path = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'AdPurifier' / 'logs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_writable_output(root=None):
    """결과 폴더에서 실제 쓰기/교체/삭제를 검사하며 다른 위치로 우회하지 않는다."""
    chosen = Path(root) if root is not None else app_root()
    if not chosen.is_absolute():
        chosen = chosen.absolute()
    probes = []

    def create_probe(prefix):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0)
        for attempt in range(5):
            path = chosen / (prefix + os.urandom(16).hex())
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                if attempt == 4:
                    raise
            else:
                probes.append(path)
                return fd, path

    try:
        try:
            fd, source = create_probe('.adpurifier-write-')
            with os.fdopen(fd, 'wb') as stream:
                stream.write(b'AdPurifier output permission probe\n')
                stream.flush()
                os.fsync(stream.fileno())
            fd, target = create_probe('.adpurifier-replace-')
            os.close(fd)
            source.replace(target)
            target.unlink()
        finally:
            for probe in probes:
                probe.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f'결과 폴더에 파일을 저장할 수 없습니다: {chosen}. '
                      'EXE 옆에 결과를 저장해야 합니다. 쓰기 권한을 확인하거나 '
                      '사용자 쓰기 가능한 폴더에 다시 설치하세요.') from exc
    return chosen


def bootstrap():
    os.environ.setdefault('OMP_NUM_THREADS', '2')
    os.environ.setdefault('MKL_NUM_THREADS', '2')
    os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(app_root() / 'runtime/chromium')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
