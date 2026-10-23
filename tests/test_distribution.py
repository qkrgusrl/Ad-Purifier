"""windows-installer-20261018: 오프라인 배포 계약. 실제 Windows 빌드 증거는 아님.

verify_assets(root, manifest)는 {files: [{path, size, sha256}]}를 검증하며
잘못된 자산에는 ValueError/OSError를 발생시킨다. fixture는 무결성 검사용
작은 바이너리일 뿐이며 실행 가능한 PE나 실제 GGUF로 가장하지 않는다.
"""
import ast
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import socket
import struct
import sys
import tempfile
import types
import unittest
import zipfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    "assets.lock.json", "packaging/AdPurifier.spec", "packaging/installer.iss",
    "scripts/build_windows.ps1", ".github/workflows/windows-installer.yml", "app.py",
)


def required_module(name):
    path = ROOT / (name.replace(".", "/") + ".py")
    assert path.is_file(), f"배포 계약 미구현: {path.relative_to(ROOT)} 필요"
    importlib.invalidate_caches()
    return importlib.import_module(name)


def pe_import_names(blob):
    """오프라인 공식 PE의 import directory만 읽는다. DLL을 로드하지 않는다."""
    assert blob[:2] == b"MZ"
    pe = struct.unpack_from("<I", blob, 0x3c)[0]
    assert blob[pe:pe + 4] == b"PE\0\0"
    count = struct.unpack_from("<H", blob, pe + 6)[0]
    optional_size = struct.unpack_from("<H", blob, pe + 20)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", blob, optional)[0]
    assert magic in (0x10b, 0x20b)
    sections = optional + optional_size

    def offset(rva):
        for i in range(count):
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", blob, sections + i * 40 + 8)
            if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
                return raw_offset + rva - virtual_address
        raise AssertionError(f"PE RVA를 해석할 수 없음: {rva}")

    import_rva = struct.unpack_from("<I", blob, optional + (112 if magic == 0x20b else 96) + 8)[0]
    cursor = offset(import_rva)
    names = set()
    while any(blob[cursor:cursor + 20]):
        name = offset(struct.unpack_from("<I", blob, cursor + 12)[0])
        names.add(blob[name:blob.index(b"\0", name)].decode("ascii").lower())
        cursor += 20
    return names


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), *sys.path]))
        self.tmp = tempfile.TemporaryDirectory(prefix="광고 청정기 ")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.install = self.base / "설치 폴더"
        self.resources = self.base / "압축 해제 리소스"
        self.cwd = self.base / "다른 작업 폴더"
        for folder in (self.install, self.resources, self.cwd):
            folder.mkdir()
        self.enterContext(patch.dict(os.environ, {k: v for k, v in os.environ.items()
                          if not k.startswith("AD_PURIFIER_")}, clear=True))
        old = Path.cwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.cwd)
        # 향후 import 부작용도 실제 모델/네트워크로 이어지지 못하게 한다.
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("실행 금지")))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("네트워크 금지")))
        self.enterContext(patch.object(socket.socket, "bind", side_effect=AssertionError("서버 금지")))

    def frozen(self):
        self.enterContext(patch.object(sys, "frozen", True, create=True))
        self.enterContext(patch.object(sys, "executable", str(self.install / "AdPurifier.exe")))
        self.enterContext(patch.object(sys, "_MEIPASS", str(self.resources), create=True))

    def test_required_distribution_artifacts_exist(self):
        missing = [name for name in ARTIFACTS if not (ROOT / name).is_file()]
        self.assertEqual(missing, [], f"설치형 배포 계약 파일 부재: {missing}")

    def test_existing_detector_default_uses_bundled_advanced(self):
        # 현재 실행 경로 자체를 평가한다. playwright는 import만 대체하고 호출 금지.
        if importlib.util.find_spec("playwright") is None:
            parent = types.ModuleType("playwright")
            child = types.ModuleType("playwright.async_api")
            child.async_playwright = lambda: self.fail("브라우저 실행 금지")
            self.enterContext(patch.dict(sys.modules, {"playwright": parent, "playwright.async_api": child}))
        # 다른 테스트가 import한 detector.main의 전역값/의존성을 오염시키지 않는다.
        spec = importlib.util.spec_from_file_location("detector._distribution_subject", ROOT / "detector/main.py")
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
        self.assertEqual(Path(main.DEFAULT_MODEL), ROOT / "models/laya-illegal-ad(advanced)",
                         "기존 detector 기본 경로가 동봉된 Advanced 모델을 선택해야 함")
        self.assertEqual(main.app_dir(), ROOT, "결과 기본 위치는 CWD가 아닌 프로젝트 루트")

    def test_development_roots_and_model_paths_ignore_cwd(self):
        self.enterContext(patch.object(sys, "frozen", False, create=True))
        paths = required_module("app_paths")
        self.assertEqual(Path(paths.app_root()), ROOT)
        self.assertEqual(Path(paths.resource_root()), ROOT)
        self.assertEqual(Path(paths.laya_model()), ROOT / "models/laya-illegal-ad(advanced)")
        self.assertEqual(Path(paths.gemma_model()), ROOT / "models/gemma/gemma-4-E2B_q4_0-it.gguf")
        suffix = ".exe" if sys.platform == "win32" else ""
        self.assertEqual(Path(paths.llama_server()), ROOT / ("runtime/llama/llama-server" + suffix))

    def test_frozen_paths_keep_assets_and_results_beside_executable(self):
        self.frozen()
        paths = required_module("app_paths")
        self.assertEqual(Path(paths.app_root()), self.install)
        self.assertEqual(Path(paths.resource_root()), self.resources)
        self.assertEqual(Path(paths.laya_model()), self.install / "models/laya-illegal-ad(advanced)")
        self.assertEqual(Path(paths.gemma_model()), self.install / "models/gemma/gemma-4-E2B_q4_0-it.gguf")
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(Path(paths.llama_server()), self.install / "runtime/llama/llama-server.exe")

    def test_explicit_relative_laya_override_is_relative_to_app_not_cwd(self):
        self.frozen()
        paths = required_module("app_paths")
        with patch.dict(os.environ, {"AD_PURIFIER_MODEL": "대체 모델/long2"}):
            self.assertEqual(Path(paths.laya_model()), self.install / "대체 모델/long2")
        with patch.dict(os.environ, {"AD_PURIFIER_MODEL": str(self.base / "절대 모델")}):
            self.assertEqual(Path(paths.laya_model()), self.base / "절대 모델")

    def test_bootstrap_leaves_cpu_threads_unset_and_defaults_tokenizer_parallelism(self):
        paths = required_module("app_paths")
        expected = {"OMP_NUM_THREADS": None, "MKL_NUM_THREADS": None,
                    "TOKENIZERS_PARALLELISM": "false"}
        before = dict(os.environ)
        with patch.dict(os.environ, {k: v for k, v in before.items() if k not in expected}, clear=True):
            paths.bootstrap()
            actual = {key: os.environ.get(key) for key in expected}
        self.assertEqual(dict(os.environ), before, "bootstrap 테스트가 기존 환경을 복구하지 않음")
        self.assertEqual(actual, expected, "미지정 CPU 스레드는 그대로 두고 토크나이저 기본값만 설정해야 함")

    def test_bootstrap_preserves_explicit_cpu_and_tokenizer_environment(self):
        paths = required_module("app_paths")
        before = dict(os.environ)
        # true도 검사해 TOKENIZERS_PARALLELISM을 무조건 false로 덮는 구현을 배제한다.
        for parallelism in ("false", "true"):
            with self.subTest(tokenizers_parallelism=parallelism):
                configured = {"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                              "TOKENIZERS_PARALLELISM": parallelism}
                with patch.dict(os.environ, configured):
                    paths.bootstrap()
                    actual = {key: os.environ.get(key) for key in configured}
                self.assertEqual(dict(os.environ), before, "bootstrap 테스트가 기존 환경을 복구하지 않음")
                self.assertEqual(actual, configured, "사용자가 지정한 환경값은 setdefault 방식으로 보존해야 함")

    def test_release_version_matches_installer_app_version(self):
        main = required_module("detector.main")
        installer = self.text("packaging/installer.iss")
        definitions = dict(re.findall(r'^\s*#define\s+(\w+)\s+"([^"\r\n]+)"\s*$',
                                      installer, flags=re.MULTILINE))
        section = None
        versions = []
        for raw in installer.splitlines():
            line = raw.strip()
            if not line or line.startswith((";", "#")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line.lower()
            elif section == "[setup]" and "=" in line:
                key, value = line.split("=", 1)
                if key.strip().lower() == "appversion":
                    versions.append(value.strip())
        self.assertEqual(len(versions), 1, "설치 스크립트 [Setup]의 AppVersion 선언이 정확히 하나여야 함")
        installer_version = re.sub(r"\{#(\w+)\}", lambda match: definitions[match.group(1)], versions[0])
        self.assertTrue(installer_version)
        self.assertNotIn("{#", installer_version, "설치 버전 전처리 상수를 해석하지 못함")
        self.assertEqual(main.TOOL_VERSION, installer_version,
                         "result.json에 사용하는 TOOL_VERSION이 설치 앱의 AppVersion과 다름")

    def test_postgate_readiness_accepts_official_b11429_cpu_dll_names(self):
        ui = required_module("ui.server")
        evidence = ROOT.parent / "build_review/20261018/windows-llama-files.json"
        # 로컬 독립 검증은 실제 공식 manifest. Windows checkout은 동일한 잠금 목록 사용.
        manifest = json.loads((evidence if evidence.is_file() else ROOT / "assets.lock.json").read_text())
        names = {Path(r["path"]).name for r in manifest["files"] if r["path"].startswith("runtime/llama/")}
        self.assertTrue({"ggml-cpu-x64.dll", "ggml-cpu-sse42.dll"} <= names)
        self.assertNotIn("ggml-cpu.dll", names, "공식 b11429 manifest에 없는 DLL을 fixture에 추가하면 안 됨")
        runtime = self.install / "runtime/llama"
        runtime.mkdir(parents=True)
        # CRT 결함과 CPU DLL 이름 결함을 분리하기 위해 app-local CRT는 이 fixture에 제공.
        for name in names | {"MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"}:
            (runtime / name).write_bytes(b"\x00fixture\xff")
        laya = self.install / "models/laya-illegal-ad(advanced)"
        for name in ("model.safetensors", "rl_agent_config.json", "questions.json", "encoder/config.json",
                     "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json"):
            path = laya / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        gemma = self.install / "model.gguf"
        gemma.write_bytes(b"fixture-not-a-model")
        with patch.object(ui, "DEFAULT_MODEL", laya), patch.object(ui, "gemma_model", return_value=gemma), \
                patch.object(ui, "llama_server", return_value=runtime / "llama-server.exe"), \
                patch.object(sys, "platform", "win32"), patch.object(ui.importlib.util, "find_spec", return_value=object()):
            readiness = ui.model_readiness()
        missing = [name for name, present in readiness["files"].items() if not present]
        self.assertEqual(missing, [], f"실제 공식 Windows CPU DLL 집합을 준비 불가로 오판: {missing}")
        self.assertTrue(readiness["files_present"])

    def test_postgate_official_pe_imports_require_crt_family(self):
        required = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
        archive = ROOT.parent / "build_review/20261018/llama-b11429-bin-win-cpu-x64.zip"
        if not archive.is_file():
            self.skipTest("공식 PE ZIP 로컬 검증자료 없음; 복사/설치 manifest는 packaging 행동 테스트가 검증")
        with zipfile.ZipFile(archive) as bundle:
            actual = pe_import_names(bundle.read("llama-server-impl.dll"))
        self.assertTrue(required <= actual, f"공식 PE import 증거와 CRT 계약 불일치: {actual}")

    def test_postgate_installed_child_module_audit_checks_actual_pid_and_paths(self):
        smoke = required_module("scripts.self_test")
        audit = getattr(smoke, "verify_child_runtime_modules", None)
        self.assertTrue(callable(audit),
                        "Windows 설치 self-test의 공개 verify_child_runtime_modules(pid, runtime_dir) 감사 계약 부재")
        runtime = self.install / "runtime/llama"
        runtime.mkdir(parents=True)
        required = ("llama-server-impl.dll", "MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
        for name in required:
            (runtime / name).write_bytes(b"fixture")
        child_pid = 424242
        for fault in (None, "system", "internal", "missing"):
            with self.subTest(fault=fault):
                paths = [runtime / name for name in required]
                if fault in ("system", "internal"):
                    paths[-1] = self.base / ("Windows/System32" if fault == "system" else "_internal") / required[-1]
                if fault == "missing":
                    paths.pop()
                process = Mock()
                process.exe.return_value = str(runtime / "llama-server.exe")
                process.memory_maps.return_value = [types.SimpleNamespace(path=str(p)) for p in paths]
                with patch("psutil.Process", return_value=process) as get_process:
                    if fault is None:
                        audit(child_pid, runtime)
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            audit(child_pid, runtime)
                get_process.assert_called_with(child_pid)
                process.memory_maps.assert_called()

    def test_child_module_audit_allows_windows_os_crt_beside_app_local_140(self):
        audit = required_module("scripts.self_test").verify_child_runtime_modules
        runtime = self.install / "runtime/llama"
        runtime.mkdir(parents=True)
        app_local = ("llama-server-impl.dll", "MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
        paths = [runtime / name for name in app_local]
        paths += [self.base / "Windows/System32" / name for name in ("msvcp_win.dll", "ucrtbase.dll")]
        process = Mock()
        process.exe.return_value = str(runtime / "llama-server.exe")
        process.memory_maps.return_value = [types.SimpleNamespace(path=str(p)) for p in paths]
        print("RAW os_crt_modules " + json.dumps([str(p) for p in paths], ensure_ascii=False), flush=True)
        with patch("psutil.Process", return_value=process) as get_process:
            result = audit(424242, runtime)
        get_process.assert_called_once_with(424242)
        process.memory_maps.assert_called_once()
        self.assertIs(result["app_local_verified"], True,
                      "Windows OS DLL은 System32에서 허용하고 versioned 140 CRT만 app-local 요구")

    def assets(self):
        fixture = {
            "models/laya-illegal-ad(advanced)/config.json": b'{"model_type":"bert"}',
            "models/laya-illegal-ad(advanced)/model.safetensors": b"\x00\x01fixture-weights\xff",
            "models/gemma/gemma-4-E2B_q4_0-it.gguf": b"\x00fixture-not-a-real-GGUF\xff",
            "runtime/llama/llama-server.exe": b"\x00fixture-not-a-PE\xff",
            "runtime/llama/ggml.dll": b"\x00fixture-DLL\xfe",
            "runtime/chromium/chrome.exe": b"\x00fixture-browser\xfd",
            "ui/static/index.html": "<!doctype html><title>광고청정기</title>".encode(),
            "detector/extract.js": b"() => ({texts: [], links: []})",
        }
        records = []
        for name, data in fixture.items():
            dest = self.install / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            records.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return {"files": records}

    def test_public_asset_validator_accepts_complete_unicode_install(self):
        manifest = self.assets()
        required_module("scripts.assets").verify_assets(self.install, manifest)

    def test_public_asset_validator_rejects_missing_corrupt_and_wrong_size(self):
        verify = required_module("scripts.assets").verify_assets
        manifest = self.assets()
        item = manifest["files"][2]
        path = self.install / item["path"]
        original = path.read_bytes()
        for fault in ("missing", "digest", "size"):
            with self.subTest(fault=fault):
                path.write_bytes(original)
                broken = copy.deepcopy(manifest)
                if fault == "missing":
                    path.unlink()
                elif fault == "digest":
                    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                    self.assertEqual(path.stat().st_size, item["size"])
                else:
                    broken["files"][2]["size"] += 1
                with self.assertRaises((ValueError, OSError)):
                    verify(self.install, broken)

    def test_public_asset_validator_rejects_escape_even_with_matching_digest(self):
        verify = required_module("scripts.assets").verify_assets
        manifest = self.assets()
        outside = self.base / "outside.bin"
        outside.write_bytes(b"outside")
        for name in ("../outside.bin", "..\\outside.bin", str(outside),
                     "C:\\Users\\outside.bin", "\\\\server\\share\\outside.bin"):
            with self.subTest(path=name):
                broken = copy.deepcopy(manifest)
                broken["files"].append({"path": name, "size": 7,
                                       "sha256": hashlib.sha256(b"outside").hexdigest()})
                with self.assertRaises((ValueError, OSError)):
                    verify(self.install, broken)

    @unittest.skipIf(sys.platform == "win32", "Windows symlink 생성 권한에 의존하지 않는 별도 경로 탈출 검증")
    def test_public_asset_validator_rejects_symlink_escape(self):
        verify = required_module("scripts.assets").verify_assets
        manifest = self.assets()
        outside = self.base / "outside.bin"
        outside.write_bytes(b"outside")
        (self.install / "escaped.bin").symlink_to(outside)
        manifest["files"].append({"path": "escaped.bin", "size": 7,
                                  "sha256": hashlib.sha256(b"outside").hexdigest()})
        with self.assertRaises((ValueError, OSError)):
            verify(self.install, manifest)

    def test_asset_lock_has_per_file_hashes_and_all_required_families(self):
        path = ROOT / "assets.lock.json"
        self.assertTrue(path.is_file(), "자산 잠금 파일 부재")
        lock = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("/Users/", json.dumps(lock))
        records = lock["files"]
        self.assertTrue(records)
        seen = set()
        for entry in records:
            name = entry["path"]
            self.assertNotIn(name, seen)
            seen.add(name)
            self.assertFalse(PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive)
            self.assertNotIn("..", PurePosixPath(name.replace("\\", "/")).parts)
            self.assertIs(type(entry["size"]), int)
            self.assertGreater(entry["size"], 0)
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
        for prefix in ("models/laya-illegal-ad(advanced)/", "models/gemma/", "runtime/llama/", "runtime/chromium/"):
            self.assertTrue(any(p.startswith(prefix) for p in seen), prefix)
        self.assertIn("models/gemma/gemma-4-E2B_q4_0-it.gguf", seen)
        self.assertIn("runtime/llama/llama-server.exe", seen)
        self.assertTrue(any(p.startswith("runtime/llama/") and p.endswith(".dll") for p in seen))
        self.assertTrue({"ui/static/index.html", "detector/extract.js"} <= seen)

    def text(self, name):
        path = ROOT / name
        self.assertTrue(path.is_file(), f"배포 계약 파일 부재: {name}")
        return path.read_text(encoding="utf-8-sig")

    def test_inno_installs_without_admin_and_spans_github_sized_parts(self):
        sections = {}
        current = None
        for raw in self.text("packaging/installer.iss").splitlines():
            line = raw.strip()
            if not line or line.startswith((";", "#")):
                continue
            if line.startswith("["):
                current = line.lower()
                sections[current] = []
            elif current:
                sections[current].append(line)
        setup = dict(line.split("=", 1) for line in sections["[setup]"] if "=" in line)
        setup = {k.strip().lower(): v.strip() for k, v in setup.items()}
        self.assertEqual(setup["defaultdirname"].lower(), r"{localappdata}\Programs\AdPurifier".lower())
        self.assertEqual(setup["privilegesrequired"].lower(), "lowest")
        self.assertEqual(setup["diskspanning"].lower(), "yes")
        self.assertEqual(int(setup["diskslicesize"]), 1800000000)
        self.assertLess(int(setup["diskslicesize"]), 2 ** 31)
        files = "\n".join(sections["[files]"]).lower()
        self.assertIn("{app}", files)
        self.assertIn("recursesubdirs", files, "모델/runtime 하위 파일을 설치해야 함")
        self.assertRegex(files, r'source\s*:\s*"[^"\n]+\\\*', "완성된 번들의 전체 트리 설치 필요")

    def test_pyinstaller_spec_includes_ui_and_javascript(self):
        tree = ast.parse(self.text("packaging/AdPurifier.spec"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self.assertTrue(any(isinstance(n.func, ast.Name) and n.func.id == "Analysis" for n in calls))
        strings = [n.value.replace("\\", "/") for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertTrue(any("app.py" in s for s in strings))
        self.assertTrue(any("static" in s for s in strings), "UI 데이터 누락")
        self.assertTrue(any("extract.js" in s for s in strings), "크롤러 JavaScript 데이터 누락")

    def test_windows_build_has_real_unit_build_and_self_test_gates(self):
        workflow = self.text(".github/workflows/windows-installer.yml")
        build = self.text("scripts/build_windows.ps1")
        active = "\n".join(line for line in workflow.splitlines() if not line.lstrip().startswith("#"))
        self.assertRegex(active, r"runs-on:\s*windows-[\w.]+")
        self.assertIn("build_windows.ps1", active)
        script = "\n".join(line for line in build.splitlines() if not line.lstrip().startswith("#"))
        # 유닛 검증 뒤 Windows에서 실제 빌드. 이 테스트는 Windows 성공을 주장하지 않는다.
        combined = active + "\n" + script
        unit = re.search(r"(?:pytest|unittest)", combined, re.I)
        self.assertIsNotNone(unit, "Windows 빌드 전에 유닛 테스트 게이트 필요")
        workflow_unit = re.search(r"(?:pytest|unittest)", active, re.I)
        workflow_build = active.index("build_windows.ps1")
        script_unit = re.search(r"(?:pytest|unittest)", script, re.I)
        script_build = re.search(r"pyinstaller", script, re.I)
        self.assertTrue(
            (workflow_unit is not None and workflow_unit.start() < workflow_build)
            or (script_unit is not None and script_build is not None and script_unit.start() < script_build.start()),
            "유닛 테스트 실행 순서가 빌드보다 앞이어야 함",
        )
        self.assertRegex(script, r"(?i)pyinstaller")
        self.assertRegex(script, r"(?i)(iscc|compile-inno)")
        self.assertIn("--self-test", combined)
        self.assertIn("--self-test-output", combined)
        self.assertIn("--no-browser", combined)
        self.assertNotRegex(combined, r"(?i)(continue-on-error:\s*true|--simulate-exe)")

    def test_app_declares_self_test_and_no_browser_cli_contract(self):
        tree = ast.parse(self.text("app.py"))
        flags = {arg.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"
                 for arg in n.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)}
        self.assertTrue({"--self-test", "--self-test-output", "--no-browser"} <= flags,
                        "app.py가 배포 self-test와 UI 자동열기 억제 옵션을 제공해야 함")


if __name__ == "__main__":
    unittest.main()
