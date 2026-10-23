# Windows 모델 포함 설치본

대상은 Windows x64, Python 3.12, CPU 전용이다. 설치 후 사용자 Python이나 HF/Playwright 캐시,
추가 모델 다운로드가 필요하지 않도록 구성한다. 실제 설치 동작은 Windows CI 결과로 확인한다. 기본 설치 위치는 `%LOCALAPPDATA%\Programs\AdPurifier`이며
관리자 권한을 요구하지 않는다. 결과 기본 위치는 EXE 폴더다.

## 고정 입력

- Gemma E2B: `assets.lock.json`의 공개 HF revision과 GGUF SHA256.
- v15 프롬프트와 source-ID 복원: `local_llm/`. 다른 벤치 버전/target-v8 선택 기능 없음.
- Laya Advanced: 팀 공유 폴더의 `laya-illegal-ad(advanced).zip`.
  ZIP의 `laya-illegal-ad-v3/`를 `models/laya-illegal-ad(advanced)/`로 매핑한다.
  원본 ZIP과 각 파일의 size/SHA256은 `assets.lock.json`으로 검증한다.
  빌드 시 `scripts/build_windows.ps1 -SingleInstaller -LayaZip <원본 ZIP 경로>`를 사용한다.
  CI에서는 저장소 변수 `LAYA_ADVANCED_ZIP_URL`에 다운로드 가능한 원본 ZIP 주소를 준비해야 한다.
  URL은 빌드에만 쓰이며 배포 앱은 모델을 다운로드하지 않는다. SHA256 불일치 시 빌드를 중단한다.
  설치 사용자는 ZIP이나 모델을 따로 받을 필요가 없다. 입력 원본 ZIP은 삭제하지 않는다.
- Windows llama CPU ZIP, Chromium/headless-shell/FFmpeg/winldd:
  공식 배포 ZIP을 실제 다운로드하여 계산한 파일별 size/SHA256을 `assets.lock.json`에 보존.
- Python wheel: `requirements-windows.lock.txt`의 정확한 버전과 실제 SHA256.
  torch는 PyTorch 공식 CPU wheel URL을 직접 잠근다. CUDA 패키지를 설치하지 않는다.
- CRT: VS 2022 설치의 최신 숫자 버전 `VC/Redist/MSVC/<version>/x64/Microsoft.VC143.CRT`
  디렉터리 하나에서 release DLL 세트 전체를 복사한다. 다른 family나 System32에서 보충하지 않는다.
  CRT는 소스 버전/해시 고정 입력이 아니다. CI가 실제 선택한 family·파일 버전·크기·SHA256을
  `crt-manifest.json`에 기록하고, 최종 `installed-assets.json`에도 실제 설치 바이트를 포함한다.
  소스 `assets.lock.json`에는 미검증 CRT 행을 추가하지 않는다.

## CI 순서

`.github/workflows/windows-installer.yml`은 `build/windows-*` 태그 push 및 `workflow_dispatch` 수동 실행을 지원한다.
일반 commit의 브랜치 push와 PR에는 빌드가 실행되지 않는다.
현재 workflow는 feature 브랜치에만 있어 `workflow_dispatch`의 default branch 조건을 충족하지 않으므로,
코드 검증 후 정확한 commit에 매번 새 `build/windows-날짜-순번` 태그를 붙여 push하는 수동 트리거를 사용한다.
원격 workflow가 비활성 상태이면 태그를 push해도 실행되지 않는다.
Windows-2022 runner에서 잠금 의존성 설치와 `pip check` 후 `scripts/build_windows.ps1`을 실행한다.
스크립트는 잠금 Chromium 준비 → 전체 unittest → 브라우저 임시 이동 → 라이선스 수집 → PyInstaller → 브라우저 dist 이동 및 모델 자산 직접 다운로드 →
Inno Setup → 실제 설치 → frozen self-test → UI HTTP smoke 순서로 동작한다.

Advanced ZIP은 빌드 담당자가 위 공유 폴더에서 준비해 `-LayaZip`으로 전달한다.
단일 EXE도 현재 소스로 새로 빌드하며, 기존 릴리스의 구형 실행파일을 재포장하지 않는다.

빌드 시작 시 14 GiB 여유 공간을 검사한다. 코드부터 동결하고 큰 자산은 dist에 직접 기록한다.
원본 모델 ZIP은 보존한다. Inno 압축이 끝나면 dist를 삭제한 뒤 설치 시험을 진행한다.
전체 Windows 실행 성공은 CI 진단과 설치 JSON으로 확인해야 한다. macOS 유닛 테스트는 그 증거가 아니다.

## 설치 시험과 증거

`%LOCALAPPDATA%\설치 시험\광고청정기`에 실제 설치하고 다른 CWD에서 실행한다.
PATH에서 사용자 Python을 제거하고, 빈 HF 캐시와 오프라인 설정을 사용한다.

```powershell
AdPurifier.exe --self-test --self-test-output C:\temp\self-test.json --no-browser
AdPurifier.exe --no-browser
```

self-test는 생산 경로의 `LayaRunner`를 통해 별도 spawn 자식에서 동봉 Laya를 로드·추론하고,
종료 후 PID가 살아 있지 않은지 확인한 뒤 Gemma를 호출한다. Chromium으로 로컬 HTTP fixture도 읽는다.
Windows Gemma 자식이 살아 있는 동안 `psutil.Process(pid).memory_maps()`를 검사하여
필수 MSVCP/VCRUNTIME과 llama-server-impl.dll이 `runtime/llama`에서 로드됐는지 확인한다.
필수 DLL과 알려진 140 계열 재배포 DLL은 System32·다른 bundle 위치에서 로드되면 실패한다.
Windows OS DLL인 msvcp_win.dll·ucrtbase.dll은 허용한다. 이는 깨끗한 VM 검증을 뜻하지 않는다.
UI 스모크는 `/api/shutdown`에 JSON `{}`를 POST하고 30초 내 종료 코드 0을 확인한다.
강제 종료는 실패 후 정리에만 사용한다.
각 phase 원시 반환값/오류/시간을 JSON에 보존한다. 모델 정확도를 평가하거나 보장하는 검사가 아니다.
실사용의 Laya confidence 0.8 이상 유지 및 미처리/낮은 confidence 검증 정책을 바꾸지 않는다.
`--no-browser`는 UI의 기본 브라우저 자동 열기만 억제한다. self-test는 항상 headless Chromium을 사용한다.

UI·self-test 시작 전에 결과 폴더에서 실제 임시 파일 생성·쓰기·교체·삭제를 검사한다.
실패하면 해당 폴더와 쓰기 권한/재설치 안내를 표시하고 시작을 중단한다. 기존 결과 파일은 건드리지 않는다.
Program Files 등 쓰기 불가능한 설치 위치를 AppData 결과 경로로 자동 대체하지 않는다.

`app_paths.app_root()`는 frozen이면 EXE 부모, 개발 중이면 저장소 루트다.
`resource_root()`는 frozen이면 `sys._MEIPASS`다. 모델 환경변수의 상대경로는 app_root 기준이다.
`PLAYWRIGHT_BROWSERS_PATH`는 bootstrap에서 app_root/runtime/chromium으로 강제 지정한다.
Windows frozen 상태에서 llama-server 실행 직전에 `SetDllDirectoryW(None)`과 bundle PATH 정리를
적용하고, Popen 직후 원래 DLL 경로를 복원한다. 인수는 shell 문자열이 아닌 리스트로 전달한다.
근거: https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application

키와 서버 로그는 사용자 쓰기 가능 데이터 폴더에 저장하며, context 종료 시 소유 프로세스와 키를 정리한다.
GemmaClient deadline은 time.perf_counter() 기준 절대시각이며 점검 전체에서 공유한다. 기본 threads=2, GPU layers=0.

## 무결성과 릴리스

`python -m scripts.assets verify --root <설치경로> --manifest <manifest>`는 경로 탈출,
누락, 크기 및 SHA256 오류를 거부한다. UI/JS는 소스 변경 시 SHA가 달라지므로 빌드 시
소스와 dist를 비교하고 `installed-assets.json`을 최종 설치 트리 전체에서 생성한다.
빌드한 assets.lock.json의 UI/JS 해시도 현재 소스로 갱신하여 설치본과 릴리스에 함께 넣는다.
릴리스에서 실제 설치본 전체를 판정하는 기준은 같은 릴리스의 `installed-assets.json`이다.

preview는 `Compression=none`으로 압축 시간을 줄이는 대신 배포 파일 크기가 증가한다.
실제 크기는 Windows 패키징 결과로 확인한다. `DiskSpanning=yes`, `DiskSliceSize=1800000000`으로 분할한다.
사용자는 Setup.exe와 모든 .bin을 같은 폴더에 내려받아 설치한다.
`installer-manifest.json`과 `SHA256SUMS.txt`가 각 조각의 크기/해시 및 소스 commit을 기록한다.
Inno 성공 직후 manifest와 배포 입력을 보존한다. 패키징 완료 후에는 검사 실패에도
`validation-status.json`에 소스 SHA와 `passed`/`not_passed`를 기록하며 job 실패는 유지한다.
현재 소스 SHA와 파일 크기·해시가 검증된 설치본은 검사 실패 시에도 `v0.2.0-preview.1`의 **draft에만 보존**한다.
목록 밖 파일은 거부하며 빌드 시작 시 이전 release 산출물을 제거한다. draft 보존은 검증 승인이 아니다.
릴리스 업로드 전에 실제 원격 태그 ref를 조회한다. annotated tag는 commit까지 해석하고
빌드의 `GITHUB_SHA`와 일치하는지 검사한다. 기존 draft 여부도 확인한다.
태그 없음(404), 권한 오류(403), 서버 오류, 잘못된 ref/응답, commit 불일치, 공개 릴리스는 모두 업로드를 중단한다.
태그·draft 자동 생성이나 기존 태그 강제 이동은 수행하지 않는다.

릴리스 실행 순서:
1. 검토 완료한 최종 소스 commit을 push한다.
2. 그 commit SHA에 정확히 연결되는 `v0.2.0-preview.1` 태그와 draft 릴리스를 준비한다.
3. 같은 소스 commit SHA에 매번 새 `build/windows-날짜-순번` 태그(예: `build/windows-20261018-01`)를 붙여 push하고 설치·self-test 결과를 확인한다.
   빌드 태그와 `v0.2.0-preview.1` 태그는 같은 commit을 가리켜야 한다. 재시도에도 새 빌드 태그를 사용하며 공개 태그는 덮어쓰지 않는다.
4. 모든 검증을 통과한 업로드를 검토한 뒤 수동으로 게시한다.

수동 빌드 태그로 실행해도 릴리스 태그·draft 준비 전에는 릴리스 업로드가 차단된다.
새 버전 태그는 릴리스 계획에서 별도로 확정한 후 변경한다.
모든 검사의 실제 증거를 부모가 검토한 뒤 수동으로 게시한다. 자동 게시하지 않는다.

## 라이선스

`THIRD_PARTY_NOTICES.txt`, `licenses/`, `licenses/python-packages/`를 동봉한다.
Gemma 4 Apache 2.0, mmBERT-base MIT 선언과 upstream 카드/원문을 보존한다.
Microsoft CRT 약관 원본과 배포 고지를 포함한다. 앱 로컬 CRT의 보안 업데이트는 새 설치본으로 배포한다.
이 고지는 프로젝트의 비공개 자체 코드에 라이선스를 새로 부여하지 않는다.

제거 프로그램에는 사용자 result/report/history를 지우는 `UninstallDelete` 규칙을 두지 않는다.

## 단일 오프라인 EXE 재패키징 (preview.2)

기존 preview.1 분할 배포는 유지한다. `.github/workflows/single-installer.yml`은
`build/single-windows-*` 태그 push에서만 실행하며 Python 3.12 외 모델 빌드 의존성을 설치하지 않는다.
실행 명령은 `./scripts/build_windows.ps1 -SingleInstaller -Version 0.2.0-preview.2`다.

입력은 공개된 `v0.2.0-preview.1`의 Setup.exe, bin 4개, installer-manifest.json뿐이다.
manifest SHA256은 `b7e57e0203efba4b63c9a715cede874aa301e1fc17f1f1f2fa1c4da8db97db8e`,
payload source commit은 `dae7af58e636d3b0438910271de11081f9cbdd01`로 검사한다.
파일 5개의 크기/해시를 검증한 뒤 저장소의 `dist/AdPurifier`에 silent 설치하고
동봉 `installed-assets.json`으로 설치 바이트를 검증한다. 입력 다운로드와 루트의
생성된 unins*.exe/dat를 삭제하고 기존 공통 Inno·설치·오프라인 self-test·UI 종료 검사를 실행한다.
설치본의 앱/모델은 baseline 바이트이며 packaging commit의 앱 소스로 다시 빌드하지 않는다.
`payload-source.json`의 `source_sha`는 baseline, `packaging_commit`은 빌드의 `GITHUB_SHA`다.

단일 모드는 `DiskSpanning=no`, `Compression=lzma2/max`, `SolidCompression=yes`를 쓴다.
컴파일된 Setup.exe 하나만 허용하고 크기는 4,200,000,000 바이트 미만이어야 한다.
2 GiB 초과는 허용하지만 실제 모델의 압축률과 Inno 컴파일 결과가 상한 통과 여부를 결정한다.
단일 manifest의 `source_commit`은 packaging SHA, `payload_source_commit`은 baseline SHA다.

부모 담당자가 packaging commit과 일치하는 `v0.2.0-preview.2` 태그와 draft를 먼저 준비한다.
workflow는 실제 Windows 설치·self-test·UI 성공 및 파일 해시 확인 후 EXE를 Actions raw artifact로
14일 보관한다. 큰 EXE를 Release 자산으로 직접 업로드하지 않는다.
Release에는 2 GiB 미만의 명시된 metadata만 올린다. `download-info.json`에는 artifact URL/ID/digest와
GitHub API가 반환한 실제 만료 시각을 기록한다. 사용자는 URL에서 EXE 하나를 내려받아 설치한다.
artifact 다운로드에는 GitHub 로그인/저장소 접근이 필요하며 만료 후에는 재빌드가 필요하다.
다운로드 후 설치·실행은 오프라인으로 가능하다. 원격 태그 SHA와 draft 상태가 다르면 업로드를 중단한다.
job outputs는 payload source SHA, packaging SHA와 실제 Windows self-test/UI 통과 상태를 제공한다.
Windows 증거가 생성되기 전에는 단일 설치 성공으로 판정하지 않는다.
