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


def bootstrap():
    os.environ.setdefault('OMP_NUM_THREADS', '2')
    os.environ.setdefault('MKL_NUM_THREADS', '2')
    os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(app_root() / 'runtime/chromium')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
