"""로컬 빌드의 --laya-dir: 릴리스 ZIP 대신 Laya 모델 폴더를 같은 설치 위치에 넣는다."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import assets

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "models/laya-illegal-ad-long2"


def record(name, data):
    return {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class LocalLayaDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="laya-dir-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.install = self.tmp / "설치"
        self.model = self.tmp / "laya-illegal-ad(advanced)" / "laya-illegal-ad-v3"
        for name in assets.LAYA_REQUIRED:
            path = self.model / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"v3:" + name.encode())
        # UI/JS는 소스와 같은 바이트가 설치 트리에 있어야 한다.
        for name in ("ui/static/index.html", "detector/extract.js"):
            path = self.install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        release = {"release": "build-inputs-long2-20261018", "asset": "laya-long2-model.zip",
                   "size": 1, "sha256": "0" * 64,
                   "members": {f"model/{n}": f"{PREFIX}/{n}" for n in (*assets.LAYA_REQUIRED, "train_report.json")}}
        self.lock = {"downloads": [release], "files": [
            record(f"{PREFIX}/model.safetensors", b"long2-weights"),
            record(f"{PREFIX}/train_report.json", b"{}"),
            record("ui/static/index.html", b"stale"),
        ]}

    def test_local_dir_replaces_locked_release_model(self):
        assets.prepare(self.install, self.lock, None, self.model)
        built = json.loads((self.install / "assets.lock.json").read_text(encoding="utf-8"))
        laya = {f["path"]: f for f in built["files"] if f["path"].startswith(PREFIX + "/")}
        self.assertEqual(set(laya), {f"{PREFIX}/{n}" for n in assets.LAYA_REQUIRED},
                         "잠금된 long2 기록(train_report 포함)이 아니라 실제 복사한 v3 파일만 기록")
        assets.verify_assets(self.install, built)
        self.assertEqual((self.install / PREFIX / "model.safetensors").read_bytes(), b"v3:model.safetensors")
        self.assertEqual(built["downloads"], [{"local_laya_dir": "laya-illegal-ad-v3", "destination": PREFIX}],
                         "설치본 잠금 파일에 사용자 경로를 남기지 않음")

    def test_missing_required_file_is_rejected_before_copy(self):
        (self.model / "questions.json").unlink()
        with self.assertRaisesRegex(ValueError, "questions.json"):
            assets.prepare(self.install, self.lock, None, self.model)
        self.assertFalse((self.install / PREFIX).exists())


if __name__ == "__main__":
    unittest.main()
