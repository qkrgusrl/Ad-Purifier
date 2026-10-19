"""분할 설치 파일별 크기/SHA256과 소스 commit을 공개한다."""
import argparse
import json
import os
import re
import subprocess
from urllib.parse import quote
from pathlib import Path
from .assets import file_record


class RemoteTagMissing(RuntimeError):
    """실제 태그 ref 조회가 명시적으로 HTTP 404를 반환했다."""


def verify_remote_tag(repo, tag, expected_sha):
    """원격 ref와 annotated tag를 읽어 실제 commit을 검증한다. 태그를 수정하지 않는다."""
    if not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
        raise RuntimeError('검증 대상 commit SHA 형식 오류')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not tag:
        raise RuntimeError('저장소/태그 형식 오류')

    def fetch(endpoint, *, initial=False):
        try:
            response = subprocess.run(['gh', 'api', endpoint, '--method', 'GET'],
                                      capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('GitHub 태그 조회 실행 실패') from exc
        try:
            data = json.loads(response.stdout)
        except (TypeError, ValueError):
            data = None
        if response.returncode:
            status = str(data.get('status', '')) if isinstance(data, dict) else ''
            if not re.fullmatch(r'[1-5][0-9]{2}', status):
                match = re.search(r'HTTP\s+([1-5][0-9]{2})', response.stderr or '')
                status = match.group(1) if match else 'unknown'
            if initial and status == '404':
                raise RemoteTagMissing(f'원격 태그 없음 (HTTP 404): {tag}')
            raise RuntimeError(f'GitHub 태그 조회 실패 (HTTP {status})')
        if not isinstance(data, dict):
            raise RuntimeError('GitHub 태그 응답 JSON 형식 오류')
        return data

    reference = fetch(f'repos/{repo}/git/ref/tags/{quote(tag, safe="")}', initial=True)
    if reference.get('ref') != 'refs/tags/' + tag:
        raise RuntimeError('응답 태그 ref 불일치')
    obj = reference.get('object')
    seen = set()
    for depth in range(8):
        if not isinstance(obj, dict) or not isinstance(obj.get('sha'), str) or not re.fullmatch(r'[0-9a-f]{40}', obj['sha']):
            raise RuntimeError('GitHub tag object/SHA 형식 오류')
        sha = obj['sha']
        if obj.get('type') == 'commit':
            if sha != expected_sha:
                raise RuntimeError(f'릴리스 태그 commit 불일치: expected={expected_sha}, actual={sha}')
            return sha
        if obj.get('type') != 'tag' or sha in seen or depth == 7:
            raise RuntimeError('태그 참조 형식 오류 또는 순환/깊이 초과')
        seen.add(sha)
        annotated = fetch(f'repos/{repo}/git/tags/{sha}')
        if annotated.get('sha') != sha:
            raise RuntimeError('annotated tag object SHA 불일치')
        obj = annotated.get('object')
    raise RuntimeError('태그 해석 실패')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-tag', action='store_true')
    parser.add_argument('--repo')
    parser.add_argument('--tag')
    parser.add_argument('--expected-sha')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--version')
    parser.add_argument('--single', action='store_true')
    parser.add_argument('--payload-source')
    args = parser.parse_args()
    if args.verify_tag:
        if not all((args.repo, args.tag, args.expected_sha)):
            parser.error('--repo/--tag/--expected-sha 필요')
        print(verify_remote_tag(args.repo, args.tag, args.expected_sha))
        return
    if args.root is None or not args.version:
        parser.error('--root/--version 필요')
    parts = sorted([*args.root.glob('*Setup.exe'), *args.root.glob('*.bin')])
    if args.single:
        if not args.payload_source or not re.fullmatch(r'[0-9a-f]{40}', args.payload_source):
            raise ValueError('단일 설치본의 payload source commit SHA 필요')
        if len(parts) != 1 or parts[0].suffix != '.exe':
            raise ValueError('단일 Setup.exe만 허용; bin 금지')
        if parts[0].stat().st_size >= 4_200_000_000:
            raise ValueError('단일 설치본 4.2 GB 한도 초과')
    elif not any(p.suffix == '.bin' for p in parts) or sum(p.suffix == '.exe' for p in parts) != 1:
        raise ValueError('단일 Setup.exe와 분할 bin 필요')
    records = [file_record(p, p.name) for p in parts]
    if not args.single and any(r['size'] >= 2**31 for r in records):
        raise ValueError('GitHub 개별 자산 2 GiB 한도 초과')
    result = {'version': args.version, 'source_commit': os.environ.get('GITHUB_SHA'),
              'installation': ('Setup.exe 하나를 내려받아 실행 (모델 포함, 오프라인 설치)'
                               if args.single else 'Setup.exe와 모든 .bin 파일을 같은 폴더에 둔 후 Setup.exe 실행'),
              'files': records}
    if args.single:
        result['payload_source_commit'] = args.payload_source
    (args.root / 'installer-manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.root / 'SHA256SUMS.txt').write_text(''.join(f"{r['sha256']}  {r['path']}\n" for r in records), encoding='utf-8')


if __name__ == '__main__':
    main()
