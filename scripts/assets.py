"""고정된 배포 입력 다운로드/검증 및 설치 트리 manifest 생성."""
import argparse
import hashlib
import json
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


def safe_path(root, name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError(f'허용하지 않는 자산 경로: {name!r}')
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).drive or '..' in path.parts:
        raise ValueError(f'자산 경로 탈출: {name!r}')
    root = Path(root).resolve()
    result = root.joinpath(*path.parts).resolve()
    if result == root or not result.is_relative_to(root):
        raise ValueError(f'자산 경로 탈출: {name!r}')
    return result


def file_record(path, name):
    with Path(path).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': name, 'size': Path(path).stat().st_size, 'sha256': digest}


def verify_assets(root, manifest):
    seen = set()
    for entry in manifest['files']:
        name = entry['path']
        path = safe_path(root, name)
        if name.casefold() in seen:
            raise ValueError(f'중복 자산: {name}')
        seen.add(name.casefold())
        if type(entry['size']) is not int or entry['size'] < 0 or not re.fullmatch('[0-9a-f]{64}', entry['sha256']):
            raise ValueError(f'잘못된 자산 metadata: {name}')
        if not path.is_file() or path.stat().st_size != entry['size']:
            raise ValueError(f'누락/크기 불일치: {name}')
        if file_record(path, name)['sha256'] != entry['sha256']:
            raise ValueError(f'SHA256 불일치: {name}')


def download(entry, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.partial')
    try:
        with urllib.request.urlopen(entry['url'], timeout=120) as response, partial.open('wb') as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        verify_assets(partial.parent, {'files': [{**entry, 'path': partial.name}]})
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def extract_archive(archive, root, entry):
    mapping = entry.get('members')
    with zipfile.ZipFile(archive) as bundle:
        names = [x.filename for x in bundle.infolist() if not x.is_dir()]
        if mapping is not None and set(names) != set(mapping):
            raise ValueError('모델 ZIP 파일 집합 불일치')
        seen = set()
        for item in bundle.infolist():
            if item.is_dir():
                continue
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('ZIP symlink 금지')
            # ZIP 원래 이름과 목적지 양쪽을 확인한다.
            safe_path(root, item.filename)
            name = mapping[item.filename] if mapping is not None else entry['destination'] + '/' + item.filename
            if name.casefold() in seen:
                raise ValueError('ZIP 중복 파일')
            seen.add(name.casefold())
            dest = safe_path(root, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, dest.open('wb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)


def prepare_browsers(registry, lock):
    """DOM 테스트의 PLAYWRIGHT_BROWSERS_PATH=0 경로를 고정된 공식 자산으로 준비."""
    prefix = 'runtime/chromium/'
    for entry in lock['downloads']:
        if not entry.get('destination', '').startswith(prefix):
            continue
        with tempfile.TemporaryDirectory(prefix='adpurifier-browser-') as temp:
            archive = Path(temp) / 'browser.zip'
            download(entry, archive)
            extract_archive(archive, registry, {**entry, 'destination': entry['destination'][len(prefix):]})
    records = [{**item, 'path': item['path'][len(prefix):]} for item in lock['files'] if item['path'].startswith(prefix)]
    verify_assets(registry, {'files': records})


def prepare(root, lock, laya_zip):
    root = Path(root).resolve()
    for entry in lock['downloads']:
        if entry.get('destination', '').startswith('runtime/chromium/') and (root / 'runtime/chromium').is_dir():
            # 유닛 테스트에 사용한 브라우저를 빌드 스크립트가 이동했다. 아래에서 전부 재검증한다.
            continue
        if entry.get('release'):
            archive = Path(laya_zip).resolve()
            verify_assets(archive.parent, {'files': [{**entry, 'path': archive.name}]})
            extract_archive(archive, root, entry)
            archive.unlink()
        elif entry.get('archive'):
            with tempfile.TemporaryDirectory(prefix='adpurifier-asset-') as temp:
                archive = Path(temp) / 'asset.zip'
                download(entry, archive)
                extract_archive(archive, root, entry)
        else:
            download(entry, safe_path(root, entry['destination']))
    immutable = [x for x in lock['files'] if x['path'].startswith(('models/', 'runtime/'))]
    verify_assets(root, {'files': immutable})
    # UI/JS는 소스 변경에 따라 달라진다. 빌드 입력 자체와 비교한 뒤 최종 트리 해시를 생성한다.
    source = Path(__file__).resolve().parents[1]
    dynamic = {}
    for name in ('ui/static/index.html', 'detector/extract.js'):
        dynamic[name] = file_record(source / name, name)
        verify_assets(root, {'files': [dynamic[name]]})
    built_lock = {**lock, 'files': [dynamic.get(item['path'], item) for item in lock['files']]}
    (root / 'assets.lock.json').write_text(json.dumps(built_lock, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def generate(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    records = [file_record(p, p.relative_to(root).as_posix()) for p in sorted(root.rglob('*'))
               if p.is_file() and p.resolve() != destination]
    destination.write_text(json.dumps({'schema': 1, 'files': records}, ensure_ascii=False, indent=2), encoding='utf-8')
    verify_assets(root, {'files': records})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'browsers', 'generate', 'verify'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--laya-zip', type=Path)
    args = parser.parse_args()
    if args.action == 'generate':
        generate(args.root, args.manifest)
    else:
        lock = json.loads(args.manifest.read_text(encoding='utf-8'))
        if args.action == 'browsers':
            prepare_browsers(args.root, lock)
        elif args.action == 'prepare':
            prepare(args.root, lock, args.laya_zip)
        else:
            verify_assets(args.root, lock)


if __name__ == '__main__':
    main()
