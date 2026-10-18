"""설치형 앱 진입점. 모델 self-test는 명시적으로 요청할 때만 실행한다."""
import argparse
import json
import multiprocessing
import sys
import traceback
from pathlib import Path

from app_paths import app_root, bootstrap, data_root


def main(argv=None):
    bootstrap()
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--self-test-output', type=Path)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--no-time-limit', action='store_true')
    args = parser.parse_args(argv)
    if args.self_test:
        from scripts.self_test import run_self_test
        output = args.self_test_output or app_root() / 'self-test.json'
        return run_self_test(output)
    # ui.server.main()은 argv 인수를 받지 않는 기존 계약을 유지한다.
    from ui.server import main as serve
    ui_args = ['--port', str(args.port)]
    if args.no_browser:
        ui_args.append('--no-browser')
    if args.no_time_limit:
        ui_args.append('--no-time-limit')
    previous = sys.argv
    try:
        sys.argv = [previous[0], *ui_args]
        serve()
    finally:
        sys.argv = previous
    return 0


if __name__ == '__main__':
    multiprocessing.freeze_support()
    # windowed bootloader에서는 stdout/stderr가 None이다.
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, (data_root() / 'app.log').open('a', encoding='utf-8', buffering=1))
    try:
        raise SystemExit(main())
    except Exception:
        error = traceback.format_exc()
        (data_root() / 'startup-error.log').write_text(error, encoding='utf-8')
        print(error, file=sys.stderr)
        if getattr(sys, 'frozen', False) and '--self-test' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'시작하지 못했습니다. 로그: {data_root() / "startup-error.log"}', '광고청정기', 0x10)
        raise SystemExit(1)
