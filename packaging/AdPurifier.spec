# PyInstaller 6.22.3 / Windows CPU 빌드.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

ROOT = Path(SPECPATH).parent
assets = [(str(ROOT / 'ui/static'), 'ui/static'),
          (str(ROOT / 'detector/extract.js'), 'detector'),
          (str(ROOT / 'licenses'), 'licenses'),
          (str(ROOT / 'THIRD_PARTY_NOTICES.txt'), '.'),
          (str(ROOT / 'assets.lock.json'), '.'),
          (str(ROOT / 'requirements-windows.lock.txt'), '.')]
binaries = []
hidden = []
# Laya/transformers는 동적 import가 많다. CPU torch만 설치된 환경에서 수집한다.
for package in ('laya', 'transformers', 'tokenizers', 'safetensors', 'playwright'):
    data, binary, imports = collect_all(package)
    assets += data
    binaries += binary
    hidden += imports
for package in ('torch', 'laya', 'transformers', 'huggingface-hub', 'tokenizers', 'safetensors', 'numpy', 'playwright'):
    assets += copy_metadata(package)

a = Analysis([str(ROOT / 'app.py')], pathex=[str(ROOT)], binaries=binaries, datas=assets,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['tensorflow', 'jax', 'flax', 'torchvision', 'torchaudio', 'IPython', 'pytest', 'tests'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='AdPurifier', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          contents_directory='.')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='AdPurifier')
