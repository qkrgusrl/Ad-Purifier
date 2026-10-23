# 광고청정기 (Ad-Purifier)

공공 웹사이트의 불법광고 의심 문구를 탐지·분류하고, 근거 문구와 페이지 내 위치를 저장하는 점검 도구입니다.
공공 웹사이트 불법광고 탐지 도구 개발 공모전 제출을 위한 프로젝트입니다.
탐지 결과를 담당자가 검토하도록 지원하며, 광고를 자동 차단하거나 게시물을 삭제하지 않습니다.

## 주요 기능

- URL을 시작점으로 웹페이지를 수집하고 본문, 링크, 숨김 요소, 제목·메타 정보를 검사합니다.
- 문자 위장과 은닉 기법을 분류하고, 게시형 불법광고도 별도로 탐지합니다.
- 규칙과 Laya Advanced 분류기를 결합하고, 선별된 항목의 문맥을 Gemma E2B로 검증합니다.
- 결과 화면에서 페이지별 근거, 판정 정보, 수집 범위와 처리 상태를 확인합니다.
- JSON·CSV 내보내기, 점검 이력, 사람의 검토 상태와 메모를 지원합니다.

| 주요 탐지 유형 | 설명 |
|---|---|
| `HOMOGLYPH` | 모양이 비슷한 문자로 바꾼 문구 |
| `JAMO` | 한글 자모를 분리한 문구 |
| `TRANSPARENT` | 투명도나 배경색 등을 이용해 숨긴 문구 |
| `OFFSCREEN` | 화면 밖에 배치한 문구 |
| 게시형 등 추가 유형 | 은닉 없이 게시된 불법광고 등 |

## 처리 흐름

```text
URL 입력 → Playwright 수집 → 문구·위치 추출 및 정규화
         → 규칙 판정 + Laya Advanced 분류
         → 선별된 후보의 Gemma E2B 문맥 검증
         → 결과 저장 → 담당자 검토
```

Gemma는 Laya의 유효 판정 신뢰도 `max(p, 1-p)`가 `0.8` 미만인 항목을 대상으로 선별합니다.
Laya의 고신뢰 광고 판정과 보도·교육 등 페이지·본문의 음수 문맥 신호가 충돌하는 경우에도 예외적으로 검증합니다.
모든 후보를 Gemma로 재검사하는 방식은 아닙니다.
긴 입력은 모델 입력 한도에 맞춰 나누고, 판정 근거를 원문 범위와 연결합니다.

기본 실행은 시간 예산을 적용하는 **bounded 모드**입니다.
전체 목표 예산은 29분이며, 수집은 19분, Laya 판정은 26분 30초, Gemma 검증은 28분 30초까지의 예산을 사용합니다.
이는 실행 시간 보장이 아니며, 페이지 수에 고정 상한을 두는 방식도 아닙니다.

기본 Gemma 선별에는 문구 길이 20자 이상, 판단 곤란 점수 `0.6` 이상 등의 조건을 적용합니다.
남은 시간과 건당 예상 시간으로 검증 건수를 정하며, 문맥 충돌 후보에는 별도 선별·대체 판정 규칙이 있습니다.
Gemma 대상에서 제외되거나 예산 안에 검증하지 못한 항목은 기존 Laya·문맥 규칙에 따라 판정할 수 있습니다.
따라서 결과가 확정됐다는 표시가 모든 모델의 검증 완료를 뜻하지는 않습니다.

## 설치형 사용법

대상 환경은 **Windows 11 x64, GPU 없는 CPU PC**입니다.

1. [릴리스 페이지](https://github.com/qkrgusrl/Ad-Purifier/releases)에서 설치 파일과 해당 릴리스의 안내를 확인합니다.
2. 모델과 실행 환경이 포함된 단일 `Setup.exe`를 실행해 설치합니다.
3. 설치한 광고청정기를 실행하고, 열린 브라우저 화면에 점검할 URL을 입력합니다.
4. 탐지를 시작한 뒤 진행 상태를 확인하고, 완료 후 결과와 미확인 범위를 검토합니다.
5. 필요한 결과 파일을 내려받고, 검토 상태와 메모를 기록합니다.

설치본은 Python, Chromium, Laya Advanced, Gemma E2B Q4_0, llama.cpp CPU 실행 환경을 동봉하는 구성입니다.
설치 사용자에게 별도 Python 설치나 모델 다운로드를 요구하지 않으며, 점검할 웹사이트에 접근할 네트워크는 필요합니다.
배포본별 실제 설치·모델 실행 검증 여부는 해당 릴리스의 검증 자료를 확인해야 합니다.

## 결과 파일과 점검 이력

기본 결과 위치는 소스 실행 시 저장소 루트, 설치형 실행 시 실행 파일 폴더입니다.

| 파일·폴더 | 내용 |
|---|---|
| `result.json` | 주요 4종 기법의 광고 판정 결과 |
| `result_extra.json` | 게시형 등 추가 유형 결과. 추가 탐지 활성화 시 0건도 빈 목록으로 생성 |
| `report.json` | 후보별 판정·근거, AI 처리 정보, 수집 실패와 미확인 범위 |
| `history/<실행 ID>/` | UI에서 실행한 점검별 결과와 이력 |
| `history/<실행 ID>/review.json` | 사람이 지정한 검토 상태와 메모 |

두 결과 JSON의 최상위 구조는 `meta`와 `findings`입니다.
`meta`에는 시작 URL, 시작·종료 시각, 소요 시간, 도구 버전 등이 기록됩니다.
`findings`의 항목에는 `id`, `url`, `is_violation`, `location`, `evidence_text`, `technique`가 들어갑니다.
`location`은 페이지 내 선택자 등 근거 위치, `evidence_text`는 탐지 문구입니다.

`result.json`의 `technique`는 주요 4종 값입니다.
내부적으로 `POSTED`로 분류한 게시형 결과는 `result_extra.json`에서 `technique: "ETC"`와
`extra_finding: "게시형 불법광고(은닉 없음)"`로 저장합니다.
추가 탐지가 활성화된 기본 실행에서는 0건이어도 빈 `findings`로 저장합니다.
추가 탐지를 끈 경우에는 생성하지 않으며 이전 실행의 파일은 제거합니다.

**사람의 검토 상태와 AI 처리 상태는 서로 다릅니다.**

- 사람의 상태: 미검토, 불법광고 확인, 오탐, 조치 완료. 담당자가 결과를 확인하고 지정합니다.
- AI 처리 상태: 모델 오류, 시간 초과, 근거 연결 실패 등으로 판정을 마치지 못한 상태를 포함합니다.
- 수집 상태: 접속 실패, 미방문 페이지, 프레임 수집 실패 등 검사하지 못한 범위를 나타냅니다.

사람의 ‘미검토’는 AI가 실행되지 않았다는 뜻이 아닙니다.
AI 미처리나 미수집 영역을 정상으로 해석하지 말고, `report.json`과 화면의 보류·오류·수집 범위를 함께 확인하세요.
탐지를 중지하면 해당 점검 결과는 저장하지 않으며, 이전 결과 파일과 이력은 유지합니다.

## 소스 실행과 모델 준비

Python 3.12를 사용합니다. **모델 가중치와 네이티브 실행 환경은 Git에 포함되지 않습니다.**
저장소를 복제하는 것만으로 모델 준비가 완료되지 않습니다.

Windows에서는 저장소 루트에서 다음 명령으로 개발 환경을 준비합니다.

```bat
setup_windows.bat
```

macOS·Linux의 기본 Python 환경 준비 예시는 다음과 같습니다.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install torch laya playwright psutil
.venv/bin/python -m playwright install chromium
```

실행 전에 다음 자산을 수동 배치하거나, 모델이 동봉된 설치본을 사용하세요.
소스 실행에는 현재 OS·CPU 아키텍처에 맞는 실행 환경이 필요합니다.

| 자산 | 저장소 루트 기준 기본 경로 |
|---|---|
| Laya Advanced 전체 모델 폴더 | `models/laya-illegal-ad(advanced)/` |
| Gemma E2B Q4_0 GGUF | `models/gemma/gemma-4-E2B_q4_0-it.gguf` |
| llama.cpp 서버 및 의존 라이브러리 | `runtime/llama/llama-server` 또는 Windows의 `llama-server.exe` |
| Chromium | Playwright 설치 경로 또는 `runtime/chromium/` |

Laya Advanced는 팀 공유 원본 `laya-illegal-ad(advanced).zip`을 준비합니다.
ZIP 내부의 `laya-illegal-ad-v3/`를 위 Advanced 경로로 배치하고, 가중치·설정·questions·encoder·tokenizer를 모두 갖춰야 합니다.
Gemma의 배포 출처·고정 revision·파일 해시는 `assets.lock.json`과 `THIRD_PARTY_NOTICES.txt`를 따릅니다.
llama.cpp는 서버 실행 파일뿐 아니라 해당 배포본의 의존 라이브러리도 함께 준비하세요.
Windows DLL과 Windows용 자산 잠금 파일을 macOS·Linux 실행 환경에 그대로 적용할 수는 없습니다.

소스 실행은 다음 중 하나를 사용합니다.

```bash
# macOS / Linux
.venv/bin/python app.py
# 또는
.venv/bin/python -m ui.server --port 8780
```

```bat
rem Windows
.venv\Scripts\python.exe app.py
rem 또는
.venv\Scripts\python.exe -m ui.server --port 8780
```

기본 UI 주소는 `http://127.0.0.1:8780`입니다.
`--no-browser`는 브라우저 자동 열기를 끕니다.
`--complete-validation`은 수집 시간 한도는 유지하고 수집된 항목의 AI 판정 시간 한도를 해제하는 실험 옵션입니다.
이 옵션은 기본값이 아니며 전체 실행 시간이 길어질 수 있습니다.

외부에 준비한 모델·실행 파일은 환경변수로 지정할 수 있습니다.

- `AD_PURIFIER_MODEL`: Laya 전체 모델 폴더
- `AD_PURIFIER_GEMMA_MODEL`: Gemma GGUF 파일
- `AD_PURIFIER_LLAMA_SERVER`: llama.cpp 서버 실행 파일
- `AD_PURIFIER_GEMMA_THREADS`, `AD_PURIFIER_GEMMA_THREADS_BATCH`, `AD_PURIFIER_GEMMA_UBATCH_SIZE`: CPU 처리 설정

모델·서버의 상대 경로는 저장소 루트 또는 설치형 실행 파일 폴더를 기준으로 해석합니다.
소스 실행에서 `PLAYWRIGHT_BROWSERS_PATH`를 지정할 수 있으며, 설치형은 동봉 브라우저 경로를 사용합니다.

## 빌드와 재현

Windows 설치본 빌드, 자산 무결성 검사, 설치 후 시험은 [docs/BUILD.md](docs/BUILD.md)를 참고하세요.
빌드에는 원본 Advanced ZIP을 `-LayaZip`으로 전달해야 합니다.
오프라인 재현에도 모델과 실행 환경을 사전에 확보해야 하며, 소스 코드만으로 재현할 수 없습니다.
동봉 자산 목록과 해시는 `assets.lock.json`, 설치본 전체의 검증 기준은 해당 배포본의 `installed-assets.json`입니다.

## 사용 시 주의

- 모델 파일이 존재한다는 사실만으로 실제 추론 성공을 보장하지 않습니다.
- 실행 실패·시간 초과·수집 누락과 탐지 0건을 구분해야 합니다. 미수집 범위는 정상 판정 대상이 아닙니다.
- 규칙과 모델의 정확도는 100%가 아니며, 정상 보도·교육 문맥의 오탐과 불법광고 미탐이 발생할 수 있습니다.
- 결과는 수집한 시점과 범위에 대한 점검 자료입니다. 사이트 전체의 합법성이나 안전성을 보장하지 않습니다.
- 쓰기 가능한 결과 폴더와 충분한 저장 공간이 필요하며, 처리 시간은 CPU·메모리·사이트 응답에 따라 달라집니다.

## 라이선스 및 제3자 고지

제3자 구성요소의 출처와 라이선스는 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)와
[licenses/](licenses/)의 원문을 확인하세요.
이 고지는 프로젝트 자체 코드나 Advanced 학습 산출물에 새로운 오픈소스 라이선스를 부여하지 않습니다.
