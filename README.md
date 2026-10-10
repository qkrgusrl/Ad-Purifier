# 광고청정기 (Ad-Purifier)

> 공기청정기처럼 공공 웹사이트에 숨어 있는 불법광고를 걸러내는 탐지 도구

**공공 웹사이트 불법광고 탐지 도구 개발 공모전**(행정안전부·NIA) 출품작입니다.
웹사이트를 크롤링해 사람 눈에는 안 보이게 숨겨진 불법광고(도박·성인·불법대출·마약 등)를 찾아
요강 붙임4 형식의 `result.json`으로 출력합니다.

| 항목 | 내용 |
|---|---|
| 접수 마감 | **2026-10-23 (금) 18:00** — gongmo@aipubcon.kr |
| 적격평가 | 10/26 ~ 10/28 (정답 검출 개수 45 + 정답 정확도 35 + 검출 시간 20, 30분 상한) |
| 본심사 | 11/2 ~ 11/4 (AI 토큰 비용도 평가) |
| 평가 PC | Windows 11, **GPU 없음** |
| 탐지 대상 기법 | `HOMOGLYPH`(닮은꼴 문자) · `JAMO`(자모 분리) · `TRANSPARENT`(투명/배경색 동일) · `OFFSCREEN`(화면 밖 배치) |

요강 원문(PDF/HWPX)은 저장소에 없습니다. 팀 공유 폴더를 참고하세요.

---

## 1. 지금까지 한 작업 (2026-10-06 ~ 10-10)

### 접근 방향 (팀 합의)
- 탐지는 **로컬 규칙 + 오픈소스 모델** 기반으로 하고, AI API는 로컬로 판단하기 애매한 경우에만 최소한으로 씁니다(키는 `.env`로 분리).
- 오탐은 정확도(35점)를 깎지만 **검출 개수 배점(45점)이 더 크니** 임계값을 너무 보수적으로 잡지 않습니다.

### 탐지 파이프라인
```
진입 URL ─▶ Playwright 크롤링 ─▶ extract.js (숨김 요소·텍스트 추출)
        ─▶ textnorm (닮은꼴 복원·자모 재조합)
        ─▶ adscore 룰 점수
              ├─ 높음  → 불법광고
              ├─ 낮음  → 정상
              └─ 경계(1.5~4) → Laya 분류기로 2차 판정
        ─▶ result.json (제출 형식) + report.json (판정 근거)
```

### 오픈소스 모델 비교
- **Laya-multilingual 채택**: CPU에서 돌아가고 한국어도 지원합니다.
- **OpenJev 제외**: GPU가 필수라서 평가 PC 조건에 맞지 않습니다.
- Laya를 템플릿 학습 데이터 900문장으로 파인튜닝해 봤습니다(`training/`).

### 프로토타입 성능
| 구성 | 모의 사이트 (정답 16건) | 손으로 쓴 검증셋 (60문장) |
|---|---|---|
| 룰만 | 정밀도 1.0 / 재현율 0.75 | 재현율 0.30 |
| Laya 제로샷 | – | 정확도 0.783 |
| Laya 파인튜닝 | – | 정확도 0.833 |
| **룰 + Laya(경계만)** | **정밀도 1.0 / 재현율 1.0 (7.5초)** | – |

⚠️ **수치가 낙관적입니다.** 모의 사이트, 정답, 룰을 한 사람이 만들었습니다.
파인튜닝 모델은 템플릿이 쉬워서 2 epoch 만에 loss가 0이 됐고(과적합),
공공기관 정상 문구("놀이터 그네", "상품권 10% 할인")를 광고로 오판합니다.
그래서 Laya는 단독으로 판정하지 않고 **경계 구간에서만** 씁니다.

### 개발하면서 찾은 함정 (같은 실수 반복 금지)
- **NFKC 정규화가 호환자모(ㅅ)를 조합자모(ᄉ)로 바꿉니다.** 그래서 자모 재조합을 NFKC보다 **먼저** 하고, 호환자모 범위는 NFKC에서 제외합니다.
- **iframe 안의 `<a href="#">` 때문에 위젯 문서가 단독 페이지로 크롤링되면서 같은 오탐이 중복으로 나옵니다.** 그래서 iframe 문서 URL은 크롤링에서 제외합니다.
- **요강은 iframe `src`를 절대주소로 쓰라고 하지만 HTML 속 값은 상대경로입니다.** 그래서 채점기는 CSS 선택자 문자열이 아니라 브라우저가 해석한 `el.src`로 비교합니다.

---

## 2. 폴더 구조

```
Ad-Purifier/
├── detector/            # 탐지기 본체
│   ├── main.py          #   진입점: 크롤링 → 판정 → result.json/report.json
│   ├── extract.js       #   브라우저에서 숨김 요소·텍스트·선택자 추출
│   ├── textnorm.py      #   닮은꼴 문자 복원, 자모 재조합, 정규화
│   └── adscore.py       #   불법광고 키워드 룰 점수 / 판정 임계값
├── ui/                  # 화면 (로컬 웹 서버 + 브라우저)
│   ├── server.py        #   http://127.0.0.1:8780/ — 탐지 실행·진행률·결과·검토·CSV API
│   └── static/          #   index.html — 화면 (외부 CDN 없이 단일 파일)
├── testsite/            # 모의 공공기관 사이트 (정답 16건 + 오탐 유도 함정)
│   ├── server.py        #   http://127.0.0.1:8765/
│   └── ground_truth.json
├── eval/score.py        # 모의 채점기 (요강 채점 규칙 재현)
├── training/            # Laya 파인튜닝
│   ├── make_dataset.py  #   템플릿 조합으로 train.jsonl 생성
│   ├── train.jsonl      #   학습 데이터 900문장
│   ├── eval_set.jsonl   #   손으로 쓴 검증셋 60문장
│   └── eval_classifier.py # 룰 vs Laya 성능 비교
├── setup_windows.bat    # Windows 환경 구성
├── run_ui.bat           # Windows: 화면 실행
├── run_testsite.bat     # Windows: 모의 사이트 실행
└── run_detect.bat       # Windows: 탐지 + 채점
```

git에서 빠지는 것(`.gitignore`): `.venv/`, `models/`, `runs/`, `history/`, 루트의 `result.json`·`report.json`, `__pycache__/`, `.env`, `.DS_Store`

---

## 3. 설치

Python **3.12**가 필요합니다.

### macOS / Linux
```bash
cd Ad-Purifier
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install torch laya playwright
.venv/bin/python -m playwright install chromium
```

### Windows 11
```bat
setup_windows.bat
```
CPU 전용 torch, laya, playwright, Chromium을 설치합니다.

---

## 4. 모델 안내 — ⚠️ 저장소에 모델이 없습니다

파인튜닝한 모델(`models/laya-illegal-ad/model.safetensors`)은 **614MB**라서
GitHub의 **파일당 100MB 제한**을 넘어 저장소에 올릴 수 없습니다.
**각자 `pip install laya`로 설치한 뒤** 아래 중 하나로 쓰세요.

### 방법 A. 기본 모델 바로 쓰기 (제로샷, 가장 간단)
`pip install laya`를 하면 처음 실행할 때 HuggingFace에서 기본 모델을 자동으로 받습니다(인터넷 필요, 수백 MB).
```bash
.venv/bin/python -m detector.main http://127.0.0.1:8765/ --out runs/full \
    --model convaiinnovations/laya-multilingual
```

### 방법 B. 직접 파인튜닝해서 만들기
프로토타입과 같은 설정입니다. CPU로도 돌아가지만 시간이 걸립니다.
```bash
# 1) 학습 데이터 생성 (training/train.jsonl, 이미 들어 있으니 수정했을 때만 실행)
.venv/bin/python training/make_dataset.py

# 2) 파인튜닝 → models/laya-illegal-ad/
.venv/bin/laya-train --data training/train.jsonl \
    --base convaiinnovations/laya-multilingual \
    --out models/laya-illegal-ad \
    --epochs 3 --micro-batch 16 --grad-accum 2 --loss soft-ce --seed 0
```
`models/laya-illegal-ad`가 있으면 탐지기가 `--model` 옵션 없이 자동으로 씁니다.

### 방법 C. 모델 없이 룰만 쓰기
```bash
.venv/bin/python -m detector.main http://127.0.0.1:8765/ --out runs/rules --no-ml
```
경계 구간은 정상으로 처리합니다(재현율이 떨어짐).

---

## 5. 실행 방법

### 화면으로 실행 (기본)
```bash
.venv/bin/python testsite/server.py     # (테스트할 때) 모의 사이트 → http://127.0.0.1:8765/
.venv/bin/python -m ui.server           # 화면 → 브라우저가 자동으로 열림 (http://127.0.0.1:8780/)
```
Windows에서는 `run_ui.bat`을 실행합니다.

1. URL을 입력하고 **탐지 시작**을 누르면 진행률(수집한 페이지 수, 현재 URL)이 표시됩니다.
2. 끝나면 요약(탐지 건수, 처리 필요, 점검 페이지, 소요 시간, 기법별 건수)과 결과 목록이 나옵니다.
3. 행을 누르면 **판정 근거**를 볼 수 있습니다: 위치(CSS 선택자), 은닉 방식, 원문과 복원 문구, 걸린 키워드, 점수, 규칙/AI 중 무엇이 판정했는지.
4. 항목마다 **검토 상태**(미검토, 불법광고 확인, 오탐, 조치 완료)와 **메모**를 저장하고, 기법·상태·검색어로 걸러 볼 수 있습니다. **엑셀(CSV) 내보내기**도 됩니다.
5. 왼쪽 **점검 이력**에서 지난 점검 결과를 다시 열 수 있습니다(`history/<실행시각>/`).

`result.json`과 `report.json`은 요강대로 **구동 폴더**에 생성됩니다. 개발할 때는 프로젝트 루트, exe로 묶으면 exe가 있는 폴더입니다.

- 화면은 내 PC 안에서만 열리는 로컬 서버(`127.0.0.1`)입니다. 외부 서버에 올리지 않습니다.
- `result.json`은 탐지할 때마다 덮어쓰고, 지난 결과는 `history/`에 남습니다. 이력을 지우려면 `history/` 폴더를 삭제하세요.
- 정상 판정 후보도 보기를 켜면 탐지기가 걸러낸 후보(예: 접근성용 "본문 바로가기")까지 흐리게 표시됩니다.
- 실제 사이트를 처음 점검할 때는 최대 페이지를 20 정도로 줄여서 해 보세요.

### 명령줄로 실행

터미널 두 개를 씁니다.

```bash
# 터미널 1 — 모의 사이트 띄우기
.venv/bin/python testsite/server.py            # → http://127.0.0.1:8765/

# 터미널 2 — 탐지 (모델 옵션은 4장 참고)
.venv/bin/python -m detector.main http://127.0.0.1:8765/ --out runs/full

# 채점 (testsite/ground_truth.json 기준)
.venv/bin/python eval/score.py runs/full/result.json

# 분류기만 비교 (룰 vs Laya, 검증셋 60문장)
.venv/bin/python training/eval_classifier.py                         # 기본 모델
.venv/bin/python training/eval_classifier.py models/laya-illegal-ad  # 파인튜닝 모델
```

Windows에서는 `run_testsite.bat`을 실행한 뒤 `run_detect.bat http://127.0.0.1:8765/`를 실행합니다.

### 탐지기 옵션
| 옵션 | 기본값 | 설명 |
|---|---|---|
| `<진입URL>` | (필수) | 크롤링을 시작할 주소 |
| `--out` | 구동 폴더 (프로젝트 루트 / exe 폴더) | `result.json`, `report.json` 저장 위치 |
| `--max-pages` | 200 | 최대 크롤링 페이지 수 |
| `--concurrency` | 4 | 동시에 여는 탭 수 |
| `--timeout` | 15000 | 페이지 로딩 제한 시간(ms) |
| `--model` | `models/laya-illegal-ad` | Laya 모델 경로 또는 HuggingFace ID |
| `--no-ml` | – | Laya 없이 룰만 사용 |

---

## 6. 앞으로 할 일

### 🔴 필수 (제출 요건)
- [x] **화면(UI) 기본 버전**: URL 입력, 진행률, 결과·근거, 검토 상태·메모, CSV, 점검 이력 (`ui/`)
- [ ] **화면 다듬기**: 실무 활용성 20점 대상. 위치 하이라이트(스크린샷), 보고서 출력 등
- [ ] **Windows 11 실행 파일 패키징**: PyInstaller + Chromium + 모델(약 640MB). Mac에서는 Windows용으로 빌드할 수 없으니 **GitHub Actions의 Windows 러너**로 빌드와 스모크 테스트를 합니다.
- [x] **결과 파일 위치**: 구동 폴더(exe 폴더)에 생성되도록 변경 (`detector.main.app_dir`)
- [ ] **제출용 모델 포함 방법**: 평가 PC에서 인터넷이 된다는 보장이 없으니 exe에 모델을 넣어야 합니다.

### 🟡 정확도
- [ ] **독립 모의 사이트**: 탐지 로직을 **안 본 팀원**이 만들어야 객관적으로 검증할 수 있습니다.
- [ ] **학습 데이터 보강**: 공공기관 정상 문구(행사·할인·놀이터 등)를 넣어 오탐을 줄입니다.
- [ ] **실제 공공기관 사이트 대상 오탐 점검**

### ⚪ 정리
- [ ] bat 파일이 LF 줄바꿈이고 한글 주석이 있어 Windows에서 문제가 날 수 있습니다. `.gitattributes`에 `*.bat text eol=crlf`를 추가하고 실제 Windows에서 검증합니다.
- [ ] `training/finetune.py`로 파인튜닝 명령을 스크립트로 정리합니다.

---

## 7. 협업 규칙

- 저장소는 **비공개**로 유지합니다. 요강상 공개된 아이디어는 보호받지 못합니다.
- API 키 등 비밀값은 `.env`에만 두고 **절대 커밋하지 않습니다**.
- 모델, 실행 결과(`runs/`), 가상환경은 커밋하지 않습니다. 100MB가 넘는 파일은 push가 거부됩니다.
- 기능 단위로 브랜치를 만들고(`feat/ui`, `fix/jamo` 등) PR로 `main`에 합칩니다.
