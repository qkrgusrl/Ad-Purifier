"""분할 설치 파일별 크기/SHA256과 소스 commit을 공개한다."""
import argparse
import json
import os
from pathlib import Path
from .assets import file_record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    parts = sorted([*args.root.glob('*Setup.exe'), *args.root.glob('*.bin')])
    if not any(p.suffix == '.bin' for p in parts) or sum(p.suffix == '.exe' for p in parts) != 1:
        raise ValueError('단일 Setup.exe와 분할 bin 필요')
    records = [file_record(p, p.name) for p in parts]
    if any(r['size'] >= 2**31 for r in records):
        raise ValueError('GitHub 개별 자산 2 GiB 한도 초과')
    result = {'version': args.version, 'source_commit': os.environ.get('GITHUB_SHA'),
              'installation': 'Setup.exe와 모든 .bin 파일을 같은 폴더에 둔 후 Setup.exe 실행', 'files': records}
    (args.root / 'installer-manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.root / 'SHA256SUMS.txt').write_text(''.join(f"{r['sha256']}  {r['path']}\n" for r in records), encoding='utf-8')


if __name__ == '__main__':
    main()
