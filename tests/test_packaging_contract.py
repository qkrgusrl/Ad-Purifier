"""배포 수정 계약: 실제 파일 효과와 오프라인 gh/pwsh double.

공개 경계:
  assets.copy_crt_family(source_dir, root) -> {files, source_family}
  app_paths.ensure_writable_output(root=None) -> Path
  release_manifest.verify_remote_tag(repo, tag, expected_sha) -> peeled SHA

작은 DLL fixture는 복사/무결성 검사용이며 실행 가능한 PE가 아니다.
이 테스트는 Windows 설치 성공이나 실제 모델 실행 증거를 만들지 않는다.
"""
import builtins
from contextlib import contextmanager
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CRT = ("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG_SHA = "c" * 40
TAG = "v0.2.0-preview.1"
REPO = "fixture-owner/fixture-repo"
REF = f"repos/{REPO}/git/ref/tags/{TAG}"
REAL_POPEN = subprocess.Popen


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def record(name, data):
    return {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def ref_object(sha=SHA, kind="commit", ref=f"refs/tags/{TAG}"):
    return {"ref": ref, "object": {"type": kind, "sha": sha}}


class ContractCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), *sys.path]))
        self.enterContext(patch.object(sys, "dont_write_bytecode", True))
        self.tmp = tempfile.TemporaryDirectory(prefix="배포 계약 ")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.install = self.base / "사용자 설치 폴더"
        self.install.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.base / "appdata"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }))
        self.enterContext(patch.object(socket.socket, "connect",
                                       side_effect=AssertionError("실제 네트워크 금지")))
        self.enterContext(patch.object(socket.socket, "bind",
                                       side_effect=AssertionError("서버 시작 금지")))
        self.enterContext(patch.object(subprocess, "Popen",
                                       side_effect=AssertionError("허용하지 않은 프로세스")))

    def helper(self, module_name, name):
        module = importlib.import_module(module_name)
        function = getattr(module, name, None)
        self.assertTrue(callable(function), f"공개 행동 계약 미구현: {module_name}.{name}")
        return function

    def frozen(self):
        self.enterContext(patch.object(sys, "frozen", True, create=True))
        self.enterContext(patch.object(sys, "executable", str(self.install / "AdPurifier.exe")))


class CrtPackagingContractTests(ContractCase):
    def family(self, version="14.44.35211", missing=()):
        folder = self.base / "VS/VC/Redist/MSVC" / version / "x64/Microsoft.VC143.CRT"
        folder.mkdir(parents=True, exist_ok=True)
        payloads = {name: (version + ":" + name).encode() + b"\x00\xff"
                    for name in (*CRT, "msvcp140_1.dll", "msvcp140_2.dll", "concrt140.dll")
                    if name not in missing}
        for name, data in payloads.items():
            (folder / name).write_bytes(data)
        (folder / "source-notice.txt").write_text("비 DLL 파일", encoding="utf-8")
        return folder, payloads

    def test_complete_family_is_copied_and_final_manifest_hashes_installed_bytes(self):
        copy_family = self.helper("scripts.assets", "copy_crt_family")
        assets = importlib.import_module("scripts.assets")
        source, payloads = self.family()
        source_before = snapshot(source)
        runtime = self.install / "runtime/llama"
        runtime.mkdir(parents=True)
        (runtime / "llama-server-impl.dll").write_bytes(b"existing llama")
        metadata = copy_family(source, self.install)
        expected = {name: data for name, data in payloads.items()}
        self.assertEqual({p.name: p.read_bytes() for p in runtime.iterdir()
                          if p.name != "llama-server-impl.dll"}, expected,
                         "선택한 단일 CRT family의 모든 DLL 바이트가 실제 runtime/llama에 필요")
        self.assertEqual((runtime / "llama-server-impl.dll").read_bytes(), b"existing llama")
        self.assertEqual(snapshot(source), source_before)
        self.assertIsInstance(metadata, dict)
        self.assertIn("source_family", metadata)
        self.assertIn("14.44.35211", json.dumps(metadata["source_family"], default=str),
                      "빌드에서 선택한 VS redist 버전을 기록해야 함")
        expected_records = [record("runtime/llama/" + name, data) for name, data in payloads.items()]
        self.assertEqual(sorted(metadata["files"], key=lambda r: r["path"]),
                         sorted(expected_records, key=lambda r: r["path"]))
        manifest_path = self.install / "installed-assets.json"
        assets.generate(self.install, manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_path = {r["path"]: r for r in manifest["files"]}
        for item in expected_records:
            self.assertEqual(by_path.get(item["path"]), item,
                             "source lock의 placeholder가 아닌 설치된 DLL의 실제 해시 필요")
        assets.verify_assets(self.install, manifest)
        victim = runtime / CRT[0]
        before = victim.read_bytes()
        victim.write_bytes(bytes([before[0] ^ 1]) + before[1:])
        with self.assertRaises(ValueError):
            assets.verify_assets(self.install, manifest)

    def test_missing_required_member_rejected_before_writes_without_mixing_families(self):
        copy_family = self.helper("scripts.assets", "copy_crt_family")
        self.family("14.43.34808")  # 다른 family의 DLL로 보충하면 안 된다.
        runtime = self.install / "runtime/llama"
        runtime.mkdir(parents=True)
        (runtime / "llama-server.exe").write_bytes(b"existing executable")
        for index, absent in enumerate(CRT):
            with self.subTest(missing=absent):
                source, _ = self.family(f"14.44.3521{index}", missing=(absent,))
                before = snapshot(self.install)
                with self.assertRaises((ValueError, OSError)):
                    copy_family(source, self.install)
                self.assertEqual(snapshot(self.install), before,
                                 "필수 DLL 누락을 발견하기 전에 일부 파일을 쓰면 안 됨")

    def test_selected_family_does_not_import_sibling_architecture_or_version(self):
        copy_family = self.helper("scripts.assets", "copy_crt_family")
        selected, payloads = self.family()
        sibling, _ = self.family("14.42.34433")
        (sibling / "foreign-family.dll").write_bytes(b"wrong family")
        x86 = selected.parent.parent / "x86/Microsoft.VC143.CRT"
        x86.mkdir(parents=True)
        (x86 / CRT[0]).write_bytes(b"wrong architecture")
        copy_family(selected, self.install)
        installed = snapshot(self.install / "runtime/llama")
        self.assertEqual(installed, payloads,
                         "단일 release CRT 디렉터리에서 선택해야 하며 다른 버전/x86 혼합 금지")


@contextmanager
def denied_writes(root):
    """읽기는 실제 FS, 쓰기만 권한 오류. os.access=True여도 실패해야 한다."""
    root = root.resolve()
    real_builtin_open, real_io_open, real_os_open = builtins.open, io.open, os.open
    attempted = []

    def protected(path):
        if isinstance(path, int):
            return False
        try:
            return Path(path).resolve().is_relative_to(root)
        except TypeError:
            return False

    def deny(path):
        attempted.append(str(path))
        raise PermissionError(13, "fixture: output directory is read-only", str(path))

    def opened(real):
        def call(path, mode="r", *args, **kwargs):
            if protected(path) and any(c in mode for c in "wax+"):
                deny(path)
            return real(path, mode, *args, **kwargs)
        return call

    def os_open(path, flags, *args, **kwargs):
        if protected(path) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            deny(path)
        return real_os_open(path, flags, *args, **kwargs)

    with patch("builtins.open", side_effect=opened(real_builtin_open)), \
            patch("io.open", side_effect=opened(real_io_open)), \
            patch("os.open", side_effect=os_open), patch("os.access", return_value=True):
        yield attempted


class OutputDirectoryContractTests(ContractCase):
    def existing_results(self):
        (self.install / "result.json").write_bytes(b'{"previous":"result"}\n')
        (self.install / "result.csv").write_bytes(b"previous,export\n")
        (self.install / "settings.json").write_bytes(b'{"preserve":true}')
        return snapshot(self.install)

    def test_default_output_probe_exercises_write_replace_delete_and_preserves_files(self):
        ensure = self.helper("app_paths", "ensure_writable_output")
        self.frozen()
        before = self.existing_results()
        operations = []
        real_replace, real_rename, real_unlink = os.replace, os.rename, os.unlink

        def moving(real):
            def call(src, dst, *args, **kwargs):
                src_path, dst_path = Path(src), Path(dst)
                if src_path.is_relative_to(self.install):
                    operations.append(("replace", src_path, dst_path, src_path.read_bytes()))
                return real(src, dst, *args, **kwargs)
            return call

        def unlink(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path).is_absolute() and Path(path).is_relative_to(self.install):
                operations.append(("delete", Path(path)))
            return real_unlink(path, *args, **kwargs)

        with patch("os.replace", side_effect=moving(real_replace)), \
                patch("os.rename", side_effect=moving(real_rename)), patch("os.unlink", side_effect=unlink):
            chosen = ensure()
        self.assertEqual(chosen, self.install, "결과 기본 위치는 실행 파일 옆 app_root")
        moves = [item for item in operations if item[0] == "replace"]
        self.assertTrue(moves, "단순 mkdir/os.access 대신 실제 임시 파일 교체를 검사해야 함")
        self.assertTrue(all(item[2].is_relative_to(self.install) and item[3] for item in moves),
                        "app_root 안에 비어 있지 않은 임시 파일을 쓰고 교체해야 함")
        self.assertTrue(any(item[0] == "delete" for item in operations), "실제 probe 삭제까지 확인해야 함")
        self.assertEqual(snapshot(self.install), before, "기존 결과 보존 및 probe 잔여물 제거")
        self.assertFalse((self.base / "appdata").exists(), "결과를 AppData로 우회하면 안 됨")

    def test_output_probe_permission_error_names_path_and_never_falls_back(self):
        ensure = self.helper("app_paths", "ensure_writable_output")
        before = self.existing_results()
        with denied_writes(self.install) as attempted:
            with self.assertRaises(OSError) as caught:
                ensure(self.install)
        self.assertTrue(attempted, "실제 쓰기를 시도해 권한 실패를 검출해야 함")
        self.assertIn(str(self.install), str(caught.exception))
        self.assertEqual(snapshot(self.install), before)
        self.assertFalse((self.base / "appdata").exists())

    def test_output_probe_windows_permission_error_stops_after_one_attempt(self):
        ensure = self.helper("app_paths", "ensure_writable_output")
        before = self.existing_results()
        platform_name = os.name
        real_tempfile_os = tempfile._os
        with denied_writes(self.install) as attempted:
            # patched open/access를 그대로 위임하되 tempfile의 Windows 분기만 켠다.
            # 전역 os.name을 바꾸면 호스트의 pathlib까지 WindowsPath로 바뀐다.
            windows_os = types.SimpleNamespace(**{**vars(os), "name": "nt"})
            self.assertIs(windows_os.open, os.open)
            self.assertTrue(windows_os.path.isdir(self.install))
            self.assertTrue(windows_os.access(self.install, windows_os.W_OK))
            with patch.object(tempfile, "_os", windows_os), patch.object(tempfile, "TMP_MAX", 5):
                self.assertEqual(os.name, platform_name)
                with self.assertRaises(OSError) as caught:
                    ensure(self.install)
        self.assertIs(tempfile._os, real_tempfile_os)
        self.assertEqual(os.name, platform_name)
        self.assertEqual(snapshot(self.install), before)
        self.assertFalse((self.base / "appdata").exists())
        self.assertIn(str(self.install), str(caught.exception))
        cause = caught.exception
        chain = []
        while cause is not None:
            chain.append({"type": type(cause).__name__, "message": str(cause)})
            last_cause = cause
            cause = cause.__cause__
        print("RAW windows_permission_probe " + json.dumps({
            "python": sys.version, "host_os_name": platform_name, "tempfile_os_name": "nt",
            "TMP_MAX": 5, "attempt_count": len(attempted), "attempted": attempted,
            "exception_chain": chain, "existing_files_preserved": snapshot(self.install) == before,
        }, ensure_ascii=False), flush=True)
        self.assertEqual(len(attempted), 1,
                         "PermissionError는 이름 충돌이 아님: os.access=True여도 즉시 중단해야 함")
        self.assertIsInstance(last_cause, PermissionError,
                              "권한 오류를 재시도 한도 소진/FileExistsError로 바꾸면 안 됨")

    def test_output_probe_retries_only_bounded_exclusive_collisions_without_overwrite(self):
        ensure = self.helper("app_paths", "ensure_writable_output")
        self.existing_results()
        real_open = os.open
        for persistent in (False, True):
            with self.subTest(persistent_collision=persistent):
                before = snapshot(self.install)
                attempted, occupied = [], {}

                def exclusive_open(path, flags, *args, **kwargs):
                    target = Path(path)
                    self.assertTrue(target.is_relative_to(self.install))
                    self.assertEqual(flags & (os.O_CREAT | os.O_EXCL), os.O_CREAT | os.O_EXCL,
                                     "검사 파일은 기존 경로를 덮어쓸 수 없는 exclusive create여야 함")
                    self.assertFalse(flags & os.O_TRUNC)
                    attempted.append({"path": str(path), "flags": flags})
                    self.assertLessEqual(len(attempted), 5, "이름 충돌 재시도도 소수로 제한해야 함")
                    if persistent or len(attempted) == 1:
                        # 선택된 경로를 다른 주체가 먼저 만든 상황. 실제 O_EXCL 오류를 사용한다.
                        if target not in occupied:
                            fd = real_open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                           | getattr(os, "O_BINARY", 0), 0o600)
                            try:
                                payload = b"existing collision owner: must survive\n"
                                os.write(fd, payload)
                            finally:
                                os.close(fd)
                            occupied[target] = payload
                    return real_open(path, flags, *args, **kwargs)

                with patch.object(os, "open", side_effect=exclusive_open):
                    windows_os = types.SimpleNamespace(**{**vars(os), "name": "nt"})
                    with patch.object(tempfile, "_os", windows_os), patch.object(tempfile, "TMP_MAX", 5):
                        if persistent:
                            with self.assertRaises(OSError) as caught:
                                ensure(self.install)
                            self.assertIn(str(self.install), str(caught.exception))
                        else:
                            self.assertEqual(ensure(self.install), self.install)
                expected = {**before, **{p.relative_to(self.install).as_posix(): data
                                        for p, data in occupied.items()}}
                self.assertEqual(snapshot(self.install), expected,
                                 "기존 결과와 충돌 경로는 보존하고 자신이 만든 probe만 제거해야 함")
                self.assertGreater(len(attempted), 1, "FileExistsError에 한해 제한된 재시도 필요")
                self.assertLessEqual(len(attempted), 5)
                self.assertFalse((self.base / "appdata").exists())
                print("RAW exclusive_collision_probe " + json.dumps({
                    "persistent": persistent, "TMP_MAX": 5, "attempt_count": len(attempted),
                    "attempted": attempted, "occupied_files": [str(p) for p in occupied],
                    "existing_files_preserved": snapshot(self.install) == expected,
                }, ensure_ascii=False), flush=True)

    def test_unwritable_app_root_blocks_actual_entrypoint_before_ui_or_self_test(self):
        app = importlib.import_module("app")
        self.frozen()
        before = self.existing_results()
        for argv, label in ((["--no-browser"], "UI"), (["--self-test"], "모델 self-test")):
            with self.subTest(mode=label):
                starts = []
                fake_ui = types.ModuleType("ui.server")
                fake_ui.main = lambda: starts.append("UI")
                fake_smoke = types.ModuleType("scripts.self_test")
                fake_smoke.run_self_test = lambda output: starts.append("model") or 0
                with patch.dict(sys.modules, {"ui.server": fake_ui, "scripts.self_test": fake_smoke}), \
                        denied_writes(self.install):
                    error = None
                    try:
                        app.main(argv)
                    except OSError as exc:
                        error = exc
                self.assertEqual(starts, [], f"쓰기 불가능한 app_root인데 {label} 시작됨")
                self.assertIsNotNone(error, "앱 시작 전에 명확한 쓰기 권한 오류 필요")
                self.assertIn(str(self.install), str(error))
                self.assertEqual(snapshot(self.install), before)
                fallback = self.base / "appdata"
                self.assertFalse(any(p.name in {"result.json", "result.csv"} for p in fallback.rglob("*")),
                                 "결과 exe 옆 계약을 AppData fallback으로 바꾸면 안 됨")

    def test_writable_install_still_starts_ui_and_keeps_self_test_output_beside_exe(self):
        app = importlib.import_module("app")
        self.frozen()
        before = self.existing_results()
        starts = []
        fake_ui = types.ModuleType("ui.server")
        fake_ui.main = lambda: starts.append(("UI", list(sys.argv[1:])))
        fake_smoke = types.ModuleType("scripts.self_test")
        fake_smoke.run_self_test = lambda output: starts.append(("model", Path(output))) or 0
        with patch.dict(sys.modules, {"ui.server": fake_ui, "scripts.self_test": fake_smoke}):
            self.assertEqual(app.main(["--no-browser", "--port", "48760"]), 0)
            self.assertEqual(app.main(["--self-test"]), 0)
        self.assertEqual(starts, [("UI", ["--port", "48760", "--no-browser"]),
                                  ("model", self.install / "self-test.json")])
        self.assertEqual(snapshot(self.install), before)


class RemoteTagContractTests(ContractCase):
    def call_with_routes(self, routes, expected_error=False):
        verify = self.helper("scripts.release_manifest", "verify_remote_tag")
        calls = []

        def fake_run(command, *args, **kwargs):
            self.assertIsInstance(command, (list, tuple), "shell 문자열로 gh를 실행하면 안 됨")
            self.assertFalse(kwargs.get("shell"))
            self.assertEqual(Path(command[0]).stem, "gh")
            self.assertIn("api", command)
            endpoint = next((str(a).lstrip("/") for a in command if str(a).lstrip("/").startswith("repos/")), None)
            self.assertIn(endpoint, routes, f"허용하지 않은 GitHub 요청: {command}")
            self.assertNotIn("POST", command)
            self.assertNotIn("PATCH", command)
            self.assertNotIn("DELETE", command)
            calls.append(endpoint)
            self.assertLessEqual(len(calls), 8, "annotated tag cycle/depth를 제한해야 함")
            code, body, stderr = routes[endpoint]
            stdout = body if isinstance(body, str) else json.dumps(body)
            result = subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)
            if kwargs.get("check") and code:
                raise subprocess.CalledProcessError(code, command, output=stdout, stderr=stderr)
            return result

        with patch.object(subprocess, "run", side_effect=fake_run):
            if expected_error:
                with self.assertRaises(RuntimeError) as caught:
                    verify(REPO, TAG, SHA)
                return caught.exception, calls
            result = verify(REPO, TAG, SHA)
        self.assertEqual(result, SHA, "실제 remote tag가 가리키는 peeled commit을 반환해야 함")
        return result, calls

    def test_lightweight_correct_tag_resolves_actual_ref(self):
        _, calls = self.call_with_routes({REF: (0, ref_object(), "")})
        self.assertEqual(calls, [REF])

    def test_annotated_and_nested_tags_are_peeled_to_commit(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                tag_endpoint = f"repos/{REPO}/git/tags/{TAG_SHA}"
                routes = {REF: (0, ref_object(TAG_SHA, "tag"), "")}
                routes[tag_endpoint] = (0, {"sha": TAG_SHA, "tag": TAG, "object": {
                    "type": "tag" if nested else "commit", "sha": OTHER_SHA if nested else SHA}}, "")
                if nested:
                    routes[f"repos/{REPO}/git/tags/{OTHER_SHA}"] = (
                        0, {"sha": OTHER_SHA, "object": {"type": "commit", "sha": SHA}}, "")
                _, calls = self.call_with_routes(routes)
                self.assertEqual(calls, list(routes))

    def test_wrong_lightweight_and_annotated_commit_are_rejected(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated):
                routes = {REF: (0, ref_object(TAG_SHA if annotated else OTHER_SHA,
                                             "tag" if annotated else "commit"), "")}
                if annotated:
                    routes[f"repos/{REPO}/git/tags/{TAG_SHA}"] = (
                        0, {"sha": TAG_SHA, "object": {"type": "commit", "sha": OTHER_SHA}}, "")
                self.call_with_routes(routes, expected_error=True)

    def test_missing_ref_is_distinct_from_permission_or_service_errors(self):
        self.helper("scripts.release_manifest", "verify_remote_tag")
        errors = {}
        for status, message in ((404, "Not Found"), (403, "Forbidden"), (500, "Internal Server Error")):
            with self.subTest(status=status):
                error, calls = self.call_with_routes({
                    REF: (1, {"message": message, "status": str(status)},
                          f"gh: {message} (HTTP {status})")
                }, expected_error=True)
                self.assertEqual(calls, [REF])
                errors[status] = (type(error).__name__, str(error))
        self.assertEqual(set(errors), {404, 403, 500}, "각 조회 실패가 RuntimeError로 구분되어야 함")
        self.assertRegex(" ".join(errors[404]), r"(?i)404|missing|not.?found|없|누락")
        for status in (403, 500):
            self.assertNotEqual(errors[404], errors[status], "조회 실패를 태그 없음으로 오인하면 안 됨")

    def test_malformed_wrong_ref_noncommit_and_cycle_are_rejected(self):
        invalid = {
            "invalid_json": "{broken",
            "missing_object": {"ref": f"refs/tags/{TAG}"},
            "wrong_ref": ref_object(ref="refs/tags/another-release"),
            "noncommit": ref_object(kind="tree"),
            "invalid_sha": ref_object(sha="main"),
        }
        for label, body in invalid.items():
            with self.subTest(case=label):
                self.call_with_routes({REF: (0, body, "")}, expected_error=True)
        with self.subTest(case="cycle"):
            self.call_with_routes({
                REF: (0, ref_object(TAG_SHA, "tag"), ""),
                f"repos/{REPO}/git/tags/{TAG_SHA}": (
                    0, {"sha": TAG_SHA, "object": {"type": "tag", "sha": TAG_SHA}}, "")
            }, expected_error=True)


# 이 프로그램은 gh의 파일/네트워크 효과를 기록으로 대체한다.
# 실제 workflow run 본문과 production release helper는 수정하지 않고 실행한다.
GH_DOUBLE = r"""
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["CONTRACT_GH_LOG"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\n")
scenario = os.environ["CONTRACT_CASE"]
expected = os.environ["GITHUB_SHA"]
tag = "v0.2.0-preview.1"
tag_sha = "c" * 40
wrong = "b" * 40
def respond(value, code=0, error=""):
    print(json.dumps(value))
    if error:
        print(error, file=sys.stderr)
    raise SystemExit(code)
if args[:2] == ["release", "view"]:
    if scenario == "release_forbidden":
        respond({"message": "Forbidden"}, 1, "gh: Forbidden (HTTP 403)")
    respond({"isDraft": True, "targetCommitish": expected})
if args and args[0] == "api":
    endpoint = next((a.lstrip("/") for a in args if a.lstrip("/").startswith("repos/")), "")
    if any(a in {"POST", "PATCH", "DELETE", "--input"} for a in args):
        respond({"message": "fixture forbids tag mutation"}, 90)
    if endpoint.endswith("/git/ref/tags/" + tag):
        if scenario in {"missing", "forbidden", "server_error"}:
            status = {"missing": 404, "forbidden": 403, "server_error": 500}[scenario]
            respond({"message": "fixture lookup error", "status": str(status)}, 1, f"gh: HTTP {status}")
        if scenario == "malformed":
            print("{broken")
            raise SystemExit(0)
        obj_type = "tag" if scenario.startswith("annotated") else "commit"
        sha = tag_sha if obj_type == "tag" else (wrong if scenario == "wrong" else expected)
        ref = "refs/tags/wrong-ref" if scenario == "wrong_ref" else "refs/tags/" + tag
        respond({"ref": ref, "object": {"type": obj_type, "sha": sha}})
    if endpoint.endswith("/git/tags/" + tag_sha):
        respond({"sha": tag_sha, "tag": tag, "object": {
            "type": "commit", "sha": wrong if scenario == "annotated_wrong" else expected}})
    respond({"message": "unrecognized fixture request"}, 91)
if args[:2] == ["release", "upload"]:
    respond({"uploaded": True})
if args[:2] == ["release", "create"]:
    respond({"created": True})
respond({"message": "unrecognized fixture command"}, 92)
"""


class WorkflowUploadContractTests(ContractCase):
    def upload_body(self):
        text = (ROOT / ".github/workflows/windows-installer.yml").read_text(encoding="utf-8")
        blocks = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^(\s*)run:\s*\|[-+]?\s*$", line)
            if not match:
                continue
            indent = len(match[1])
            body = []
            for following in lines[index + 1:]:
                if following.strip() and len(following) - len(following.lstrip()) <= indent:
                    break
                body.append(following[indent + 2:] if following.strip() else "")
            block = "\n".join(body)
            if "release upload" in block or re.search(r"scripts[/\\].*(publish|upload|release)", block, re.I):
                blocks.append(block)
        self.assertEqual(len(blocks), 1, "실제 installer 업로드 run 구간을 정확히 하나 찾을 수 있어야 함")
        return blocks[0]

    def launcher(self, folder, name, script):
        """POSIX/Windows 모두 외부 서비스에 닿지 않는 명령만 PATH에 둔다."""
        if os.name == "nt":
            path = folder / (name + ".cmd")
            path.write_text(f'@"{sys.executable}" "%~dp0{script.name}" %*\r\n', encoding="utf-8")
        else:
            path = folder / name
            path.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                            + shlex.quote(str(script)) + ' "$@"\n', encoding="utf-8")
            path.chmod(0o755)

    def run_workflow(self, scenario):
        pwsh = shutil.which("pwsh")
        if not pwsh and Path("/opt/homebrew/bin/pwsh").is_file():
            pwsh = "/opt/homebrew/bin/pwsh"
        self.assertTrue(pwsh, "실제 workflow 행동 검증에 로컬 pwsh 필요")
        work = self.base / scenario
        work.mkdir()
        # YAML이 별도 production .ps1/.py를 호출하도록 바뀌어도 같은 소스로 실행.
        shutil.copytree(ROOT / "scripts", work / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (work / "release").mkdir()
        (work / "release/AdPurifier-fixture-Setup.exe").write_bytes(b"not an executable")
        (work / "release/fixture-1.bin").write_bytes(b"not a model")
        commands = work / "fixture-bin"
        commands.mkdir()
        gh_script = commands / "gh_double.py"
        gh_script.write_text(GH_DOUBLE, encoding="utf-8")
        self.launcher(commands, "gh", gh_script)
        # Windows CreateProcess의 .cmd 탐색 차이까지 제거한다. production helper의
        # subprocess.run 호출은 그대로 수행하고 gh 실행 대상만 Python double로 바꾼다.
        (work / "sitecustomize.py").write_text(
            "import os, pathlib, socket, subprocess, sys\n"
            "_run = subprocess.run\n"
            "def _offline_run(args, *pos, **kw):\n"
            "    if isinstance(args, (list, tuple)) and pathlib.Path(args[0]).stem == 'gh':\n"
            "        args = [sys.executable, os.environ['CONTRACT_GH_SCRIPT'], *args[1:]]\n"
            "    return _run(args, *pos, **kw)\n"
            "subprocess.run = _offline_run\n"
            "def _blocked(*args, **kwargs):\n"
            "    raise AssertionError('fixture forbids real network')\n"
            "socket.socket.connect = _blocked\n",
            encoding="utf-8")
        # Python helper 실행은 진짜 interpreter, gh만 double.
        python_script = commands / "python_forward.py"
        python_script.write_text(
            "import os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
            encoding="utf-8")
        self.launcher(commands, "python", python_script)
        self.launcher(commands, "python3", python_script)
        script = work / "run_upload.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n" + self.upload_body(), encoding="utf-8")
        log = work / "gh-calls.jsonl"
        # Windows os.environ은 키를 대문자로 반환한다. SystemRoot/ComSpec도 보존한다.
        # https://docs.python.org/3.12/library/subprocess.html#subprocess.Popen
        allowed_os_keys = {"HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "WINDIR",
                           "TEMP", "TMP", "TMPDIR", "PATHEXT"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed_os_keys}
        env.update({
            "PATH": str(commands), "PYTHONPATH": str(work), "PYTHONDONTWRITEBYTECODE": "1",
            "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": SHA,
            "CONTRACT_CASE": scenario, "CONTRACT_GH_LOG": str(log),
            "CONTRACT_GH_SCRIPT": str(gh_script),
            "GH_TOKEN": "offline-fixture-no-real-token", "GH_CONFIG_DIR": str(work / "gh-config"),
            "POWERSHELL_TELEMETRY_OPTOUT": "1", "POWERSHELL_UPDATECHECK": "Off",
        })
        with patch.object(subprocess, "Popen", REAL_POPEN):
            launcher = subprocess.run([pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                                       "& python --version; exit $LASTEXITCODE"],
                                      cwd=work, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(launcher.returncode, 0, launcher.stdout + launcher.stderr)
            self.assertEqual((launcher.stdout + launcher.stderr).strip(), "Python " + sys.version.split()[0],
                             "동일한 한글 경로/격리 환경에서 실제 Python launcher가 먼저 실행되어야 함")
            result = subprocess.run([pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=30)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        self.assertTrue(calls, f"gh double에 도달하지 못한 fixture 오류: {result.stderr}")
        print("RAW workflow_fixture " + json.dumps({"scenario": scenario,
              "python_launcher": (launcher.stdout + launcher.stderr).strip(),
              "launcher_returncode": launcher.returncode, "workflow_returncode": result.returncode,
              "gh_calls": calls}, ensure_ascii=False), flush=True)
        return result, calls

    def test_actual_upload_step_checks_correct_lightweight_and_annotated_tag_before_upload(self):
        for scenario in ("correct", "annotated_correct"):
            with self.subTest(case=scenario):
                result, calls = self.run_workflow(scenario)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                uploads = [i for i, args in enumerate(calls) if args[:2] == ["release", "upload"]]
                refs = [i for i, args in enumerate(calls)
                        if args[0] == "api" and any("/git/ref/tags/" in a for a in args)]
                self.assertEqual(len(uploads), 1, calls)
                self.assertTrue(refs, f"targetCommitish 외에 실제 태그 ref 조회 필요: {calls}")
                self.assertLess(max(refs), uploads[0], calls)
                if scenario.startswith("annotated"):
                    peeled = [i for i, args in enumerate(calls)
                              if args[0] == "api" and any("/git/tags/" in a for a in args)]
                    self.assertTrue(peeled, calls)
                    self.assertLess(max(peeled), uploads[0], calls)

    def test_actual_upload_step_rejects_wrong_missing_invalid_or_unavailable_tag(self):
        for scenario in ("wrong", "annotated_wrong", "missing", "forbidden", "server_error",
                         "wrong_ref", "malformed", "release_forbidden"):
            with self.subTest(case=scenario):
                result, calls = self.run_workflow(scenario)
                self.assertFalse(any(args[:2] == ["release", "upload"] for args in calls),
                                 f"{scenario}: 검증되지 않은 Git tag로 업로드 실행됨: {calls}")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(any(args[:2] == ["release", "create"] for args in calls),
                                 "기존 release/tag 검증 실패를 새 draft 생성으로 우회하면 안 됨")


if __name__ == "__main__":
    unittest.main()
