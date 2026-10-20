"""설치된 wheel의 실제 저작권/라이선스 원문을 배포 트리에 수집한다."""
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / 'licenses/python-packages'
    output.mkdir(parents=True, exist_ok=True)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise FileNotFoundError('빌드 Python LICENSE.txt 누락')
    shutil.copyfile(python_license, root / 'licenses/Python-LICENSE.txt')
    records = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = distribution.metadata['Name']
        dest = output / name
        dest.mkdir(exist_ok=True)
        retained = []
        for entry in distribution.files or []:
            # Wheel이 제공하는 원문만 복사한다. 새로운 라이선스를 생성하지 않는다.
            if any(word in entry.name.lower() for word in ('license', 'licence', 'copying', 'notice')):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    safe = dest / str(entry).replace('..', '_').replace('/', '__').replace('\\', '__')
                    shutil.copyfile(source, safe)
                    retained.append(safe.name)
        (dest / 'METADATA.txt').write_text(distribution.read_text('METADATA') or '', encoding='utf-8')
        records.append({'name': name, 'version': distribution.version, 'license_files': retained,
                        'license_expression': distribution.metadata.get('License-Expression'),
                        'license': distribution.metadata.get('License'),
                        'home_page': distribution.metadata.get('Home-page')})
    (output / 'packages.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    if not any(x['name'].lower() == 'laya' and x['license_files'] for x in records):
        raise RuntimeError('Laya 실제 라이선스 파일 누락')


if __name__ == '__main__':
    main()
