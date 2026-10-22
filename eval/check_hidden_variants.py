"""적격평가 4종의 은닉 변형 32가지(요강 예시 밖 변형 포함)를 한 페이지에 모아 탐지·기법 분류를 검사한다.

    python eval/check_hidden_variants.py            # 모의 페이지를 띄우고 탐지기를 돌린 뒤 채점
    python eval/check_hidden_variants.py --no-ml    # AI 없이 규칙만

페이지(eval/hidden_variants/index.html)의 항목마다 광고 문구가 다르다 — 같은 문구는 같은 페이지에서
한 건으로 합쳐지므로(중복 기준) 문구를 같게 쓰면 변형별 결과를 볼 수 없다.
정상 숨김 문구 5개(본문 바로가기, 검색어 입력, 도박 신고 안내 등)가 result.json에 나오면 오탐으로 센다.
"""
import argparse
import functools
import http.server
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent / "hidden_variants"
ROOT = HERE.parents[1]


def item_key(location, ids):
    m = re.search(r"section(?:#([\w.-]+)|\.v:nth-of-type\((\d+)\)|:nth-of-type\((\d+)\))", location)
    if not m:
        return location
    if m.group(1):
        return m.group(1)
    n = int(m.group(2) or m.group(3))
    return ids[n - 1] if n <= len(ids) else f"trap{n - len(ids)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ml", action="store_true")
    args = ap.parse_args()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    with tempfile.TemporaryDirectory() as out:
        cmd = [sys.executable, "-m", "detector.main", url, "--out", out] + (["--no-ml"] if args.no_ml else [])
        subprocess.run(cmd, cwd=ROOT, check=True)
        findings = json.loads((Path(out) / "result.json").read_text(encoding="utf-8"))["findings"]
    httpd.shutdown()
    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    ids = list(expected)
    got = {}
    for f in findings:
        got.setdefault(item_key(f["location"], ids), []).append(f["technique"])
    ok = 0
    for i, tech in expected.items():
        status = "OK" if tech in got.get(i, []) else ("기법 다름" if got.get(i) else "놓침")
        ok += status == "OK"
        print(f"{status:6} {i:20} 정답 {tech:11} 결과 {got.get(i, [])}")
    false_pos = {k: v for k, v in got.items() if k not in expected}
    print(f"\n정탐 {ok} / {len(expected)}, 정상 숨김 문구 오탐 {len(false_pos)} {false_pos or ''}")
    return 0 if ok == len(expected) and not false_pos else 1


if __name__ == "__main__":
    sys.exit(main())
