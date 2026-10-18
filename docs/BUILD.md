# Windows 모델 포함 설치본

대상은 Windows x64, Python 3.12, CPU 전용이다. 설치 후 사용자 Python이나 HF/Playwright 캐시,
추가 모델 다운로드가 필요하지 않다. 기본 설치 위치는 `%LOCALAPPDATA%\Programs\AdPurifier`이며
관리자 권한을 요구하지 않는다. 결과 기본 위치는 EXE 폴더다.

## 고정 입력

- Gemma E2B: `assets.lock.json`의 공개 HF revision과 GGUF SHA256.
- v15 프롬프트와 source-ID 복원: `local_llm/`. 다른 벤치 버전/target-v8 선택 기능 없음.
- Laya long2: private release `build-inputs-long2-20261018`의 `laya-long2-model.zip`.
  `model/` 접두사를 `models/laya-illegal-ad-long2/`로 매핑한다. SDK 전체를 복사하지 않는다.
- Windows llama CPU ZIP, Chromium/headless-shell/FFmpeg/winldd:
  공식 배포 ZIP을 실제 다운로드하여 계산한 파일별 size/SHA256을 `assets.lock.json`에 보존.
- Python wheel: `requirements-windows.lock.txt`의 정확한 버전과 실제 SHA256.
  torch는 PyTorch 공식 CPU wheel URL을 직접 잠근다. CUDA 패키지를 설치하지 않는다.

## CI 순서

`.github/workflows/windows-installer.yml`은 `feature/windows-installer` push 및 수동 실행을 지원한다.
Windows-2022 runner에서 잠금 의존성 설치와 `pip check` 후 `scripts/build_windows.ps1`을 실행한다.
스크립트는 잠금 Chromium 준비 → 전체 unittest → 브라우저 임시 이동 → 라이선스 수집 → PyInstaller → 브라우저 dist 이동 및 모델 자산 직접 다운로드 →
Inno Setup → 실제 설치 → frozen self-test → UI HTTP smoke 순서로 동작한다.

private 입력은 `GH_TOKEN` 환경변수를 사용하는 `gh release download`로 받는다.
토큰을 파일이나 명령 인수에 기록하지 않는다. 입력 릴리스와 빌드 workflow가 같은 저장소에 있어야 한다.
프로젝트의 다른 저장소로 옮기면 해당 입력에 대한 접근 권한을 별도로 구성해야 한다.

빌드 시작 시 14 GiB 여유 공간을 검사한다. 코드부터 동결하고 큰 자산은 dist에 직접 기록한다.
ZIP은 추출 직후 삭제한다. Inno 압축이 끝나면 dist를 삭제한 뒤 설치 시험을 진행한다.
전체 Windows 실행 성공은 CI 진단과 설치 JSON으로 확인해야 한다. macOS 유닛 테스트는 그 증거가 아니다.

## 설치 시험과 증거

`%LOCALAPPDATA%\설치 시험\광고청정기`에 실제 설치하고 다른 CWD에서 실행한다.
PATH에서 사용자 Python을 제거하고, 빈 HF 캐시와 오프라인 설정을 사용한다.

```powershell
AdPurifier.exe --self-test --self-test-output C:\temp\self-test.json --no-browser
AdPurifier.exe --no-browser
```

self-test는 실제 동봉 Laya와 Gemma를 직접 호출하고 Chromium으로 로컬 HTTP fixture를 읽는다.
각 phase 원시 반환값/오류/시간을 JSON에 보존한다. 모델 정확도를 평가하거나 보장하는 검사가 아니다.
실사용의 Laya confidence 0.8 이상 유지 및 미처리/낮은 confidence 검증 정책을 바꾸지 않는다.
`--no-browser`는 UI의 기본 브라우저 자동 열기만 억제한다. self-test는 항상 headless Chromium을 사용한다.

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

Inno는 `DiskSpanning=yes`, `DiskSliceSize=1800000000`으로 분할한다.
사용자는 Setup.exe와 모든 .bin을 같은 폴더에 내려받아 설치한다.
`installer-manifest.json`과 `SHA256SUMS.txt`가 각 조각의 크기/해시 및 소스 commit을 기록한다.
모든 게이트 성공 후에만 `v0.2.0-preview.1`에 업로드하고 **draft를 유지**한다.
기존 릴리스가 공개됐거나 다른 commit을 가리키면 덮어쓰지 않고 실패한다.
최종 검토·게시·Git 작업은 부모 담당이다. 실패 시 설치 파일은 업로드하지 않고 진단 artifact만 보존한다.

## 라이선스

`THIRD_PARTY_NOTICES.txt`, `licenses/`, `licenses/python-packages/`를 동봉한다.
Gemma 4 Apache 2.0, mmBERT-base MIT 선언과 upstream 카드/원문을 보존한다.
이 고지는 프로젝트의 비공개 자체 코드에 라이선스를 새로 부여하지 않는다.

제거 프로그램에는 사용자 result/report/history를 지우는 `UninstallDelete` 규칙을 두지 않는다.
