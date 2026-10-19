"""단일 재패키징 계약. Windows 설치/오프라인 검증은 네이티브 CI 담당."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import release_manifest

ROOT = Path(__file__).resolve().parents[1]
CURRENT = "a" * 40
PAYLOAD = "b" * 40
LIMIT = 4_200_000_000


class SingleInstallerTests(unittest.TestCase):
    def invoke(self, root, *options):
        argv = ["release_manifest", "--root", str(root), "--version",
                "0.2.0-preview.2", *options]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, GITHUB_SHA=CURRENT):
            try:
                release_manifest.main()
            except SystemExit as exc:
                self.fail(f"요청한 CLI 계약을 처리하지 못함: exit={exc.code}")

    def sparse_setup(self, root, size):
        path = root / "AdPurifier-0.2.0-preview.2-Setup.exe"
        with path.open("wb") as stream:
            stream.truncate(size)
        return path

    def test_single_manifest_accepts_large_exe_and_binds_both_commits(self):
        for size in (2**31 + 1, LIMIT - 1):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                setup = self.sparse_setup(root, size)
                record = {"path": setup.name, "size": setup.stat().st_size,
                          "sha256": "c" * 64}
                # 크기/파일 탐색은 실제 FS, 수 GiB 해시 읽기만 생략한다.
                with patch.object(release_manifest, "file_record", return_value=record):
                    self.invoke(root, "--single", "--payload-source", PAYLOAD)
                manifest = json.loads((root / "installer-manifest.json").read_text())
                self.assertEqual(manifest["source_commit"], CURRENT)
                self.assertEqual(manifest["payload_source_commit"], PAYLOAD)
                self.assertEqual(manifest["version"], "0.2.0-preview.2")
                self.assertEqual(manifest["files"], [record])
                self.assertNotIn(".bin", manifest["installation"])
                self.assertEqual((root / "SHA256SUMS.txt").read_text(),
                                 f"{record['sha256']}  {setup.name}\n")

    def test_single_rejects_ambiguous_payloads_and_invalid_provenance(self):
        cases = [("bin", PAYLOAD, 1), ("second_setup", PAYLOAD, 1),
                 ("invalid_sha", "dae7af5", 1), ("nonhex_sha", "g" * 40, 1),
                 ("missing_sha", None, 1), ("limit", PAYLOAD, LIMIT),
                 ("over_limit", PAYLOAD, LIMIT + 1)]
        for case, source, size in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                setup = self.sparse_setup(root, size)
                if case == "bin":
                    (root / "payload-1.bin").write_bytes(b"part")
                if case == "second_setup":
                    (root / "OtherSetup.exe").write_bytes(b"other")
                def record(path, name):
                    return {"path": name, "size": Path(path).stat().st_size,
                            "sha256": "c" * 64}
                options = ["--single"]
                if source is not None:
                    options += ["--payload-source", source]
                argv = ["release_manifest", "--root", str(root), "--version",
                        "0.2.0-preview.2", *options]
                # argparse의 '알 수 없는 옵션'을 입력 검증 성공으로 세지 않는다.
                stderr = io.StringIO()
                with patch.object(sys, "argv", argv), patch.object(
                        release_manifest, "file_record", side_effect=record), \
                        contextlib.redirect_stderr(stderr):
                    try:
                        release_manifest.main()
                    except (ValueError, RuntimeError):
                        pass
                    except SystemExit as exc:
                        self.assertNotEqual(exc.code, 0)
                        self.assertNotIn("unrecognized arguments", stderr.getvalue())
                    else:
                        self.fail(f"잘못된 단일 설치 입력 허용: {case}")
                self.assertFalse((root / "installer-manifest.json").exists())
                self.assertFalse((root / "SHA256SUMS.txt").exists())

    def test_default_split_manifest_still_requires_and_records_bin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "OldSetup.exe").write_bytes(b"setup")
            with self.assertRaises(ValueError):
                self.invoke(root)
            (root / "Old-1.bin").write_bytes(b"payload")
            self.invoke(root)
            manifest = json.loads((root / "installer-manifest.json").read_text())
            self.assertEqual({r["path"] for r in manifest["files"]},
                             {"OldSetup.exe", "Old-1.bin"})
            self.assertEqual(manifest["source_commit"], CURRENT)
            self.assertIn(".bin", manifest["installation"])

    def test_separate_workflow_delivers_raw_exe_and_metadata_only_release(self):
        import yaml
        path = ROOT / ".github/workflows/single-installer.yml"
        self.assertTrue(path.is_file(), "별도 single-installer.yml 필요")
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"], {"push": {"tags": ["build/single-windows-*"]}})
        steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
        uploads = [step for step in steps if step.get("uses", "").startswith(
            "actions/upload-artifact@") and "*.exe" in step.get("with", {}).get("path", "")]
        self.assertEqual(len(uploads), 1, "단일 EXE 원본 artifact 업로드 필요")
        upload = uploads[0]
        self.assertRegex(upload["uses"], r"^actions/upload-artifact@(?:v7|[0-9a-f]{40})$")
        if not upload["uses"].endswith("@v7"):
            self.assertRegex(path.read_text(), r"actions/upload-artifact@[0-9a-f]{40}\s*#.*\bv7\b")
        self.assertEqual(upload["with"].get("archive"), "false")
        self.assertEqual(upload["with"].get("retention-days"), "14")
        self.assertEqual(len(upload["with"]["path"].splitlines()), 1)
        self.assertRegex(upload["with"]["path"], r"^[^*\n]*\*\.exe$")
        release_steps = [s["run"] for s in steps if "gh release upload" in s.get("run", "")]
        self.assertTrue(release_steps, "preview.2 metadata 공개 경로 필요")
        for script in release_steps:
            self.assertIn("v0.2.0-preview.2", script)
            self.assertNotIn("v0.2.0-preview.1", script)
            commands = re.findall(r"gh release upload[^\n]*(?:`\n[^\n]*)*", script)
            for command in commands:
                self.assertNotRegex(command, r"(?i)\.exe|\.bin|release[/\\]\*")
            # 전체 release 디렉터리를 무필터로 넘기는 기존 경로를 금지한다.
            self.assertNotRegex(script, r"(?i)Get-ChildItem\s+release\s+-File\s*\|")

    def test_inno_single_branch_compresses_without_changing_default_slices(self):
        text = (ROOT / "packaging/installer.iss").read_text()
        self.assertRegex(text, r"(?m)^\s*#ifdef\s+SingleInstaller\s*$")
        def setup_values(single):
            defined = {"SingleInstaller"} if single else set()
            active, stack, lines = True, [], []
            for line in text.splitlines():
                directive = re.match(r"\s*#(ifdef|ifndef|else|endif|define)\b\s*(\w*)", line)
                if directive:
                    op, name = directive.groups()
                    if op in ("ifdef", "ifndef"):
                        condition = (name in defined) == (op == "ifdef")
                        stack.append((active, condition))
                        active = active and condition
                    elif op == "else":
                        parent, condition = stack[-1]
                        active = parent and not condition
                    elif op == "endif":
                        active = stack.pop()[0]
                    elif active:
                        defined.add(name)
                elif active:
                    lines.append(line)
            return dict(re.findall(r"(?m)^([A-Za-z]+)=(.*)$", "\n".join(lines)))
        single, split = setup_values(True), setup_values(False)
        self.assertEqual(single["DiskSpanning"].lower(), "no")
        self.assertNotEqual(single["Compression"].lower(), "none")
        self.assertEqual(single["SolidCompression"].lower(), "yes")
        self.assertRegex(single["OutputBaseFilename"], r"\{#\w+\}")
        self.assertEqual(split["DiskSpanning"].lower(), "yes")
        self.assertEqual(split["DiskSliceSize"], "1800000000")
        self.assertEqual(split["Compression"].lower(), "none")
        self.assertEqual(split["SolidCompression"].lower(), "no")
        self.assertEqual(split["OutputBaseFilename"], "AdPurifier-0.2.0-preview.1-Setup")


if __name__ == "__main__":
    unittest.main()
