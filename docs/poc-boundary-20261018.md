# 2026-10-18 규칙 경계 후보 Laya/Gemma PoC

## 현재 확인 요약

2026-10-18의 실험을 아래 네 단계로 구분한다. 각 단계의 입력·모델 호출·결과는 독립적으로 보존하며 합산해 정확도를 계산하지 않는다.

| 순서 | 실험 | 확인된 결과 | 검증 범위 |
|---|---|---|---|
| 1 | 초기 24개 평가 | Laya 대상 11개, Laya 일치 7개, Gemma 정책외 진단 일치 8개. 저신뢰 후보 0개 | 저장 HTML·규칙·모델 평가. S06/S07은 이 단계에서 모델 미실행 |
| 2 | 후속 S06/S07 production 통합 | 실제 `main.run`에서 두 대상 모두 Gemma `clean`. 잔여 12개는 Laya 정상 선별 | 동일 프로세스에서 Laya 객체 해제 후 Gemma 실행. `body-context` 분기 독립 검증은 아님 |
| 3 | 별도 16개 저신뢰 probe | 실제 Laya confidence <0.8 후보 3개 발견. Gemma 호출 0회 | 자동 후속 검증 경로의 입력 확보용. 의미 정확도 평가는 아님 |
| 4 | 고정 5개 production scan | 최종 `ef70919`에서 spawn Laya 배치 1회, Gemma verify 4회·extract 1회. R01 잔여 회수 후 광고 저장 | owner 구조 수정 포함 소스에서 완료. 이전 `4eaacf9` 결과는 별도 보존 |

**현재 결과:** owner 구조 수정이 포함된 `ef7091925e6c7eaea93e6fb144c2363f23a5b27e`에서 같은 고정 5개를 20:17:33~20:18:02 KST에 한 번 실행 완료했다. 실행 전후 소스·입력 해시가 같고 Gemma는 운영 경로의 verify 4회·extract 1회였다. [최종 보고서와 실제 원시자료](poc/20261018/final-five-case/FINAL_REPORT.md)에 결과와 종료 기록을 정리했다. 추가 사례·강제 진단·재탐색은 없었다.

## 현재 배포 정책

- 실제 배포에서 Laya `confidence >= 0.8`이면 Gemma를 우회한다. 전수 진단은 PoC에만 허용하며 배포 전수 모드는 없다.
- **초기 실험의 0건은 Laya 결과 중 `confidence < 0.8`인 후보가 없었다는 뜻이다. 전체 앱의 Gemma 호출이 0건이라는 결론이 아니다.**
- 새 배포 예외: `page-context`/`body-context` 때문에 Laya를 생략한 `borderline` 후보도 미처리 후보로 Gemma 검증에 들어간다.
- S06/S07의 예방 문맥 예외는 후속 production 실험에서 확인했다. 이 결과를 초기 24개 실험의 호출·성공 건수에 더하지 않는다.
- confidence 0.8은 운영 분기 기준이다. 정답 확률 80%가 보장된다는 뜻이 아니다.

## 1. 초기 24개 실험 — 2026-10-18 18:20 KST

기준 코드는 `e6cd33168c7544019c603af05c789c6d4bed2183`이다. 이 절은 최초 측정 결과를 보존한다.

추론 전에 원문과 gold를 고정했다. 전체 24개는 합성 21개와 저장 실사이트 링크 3개다. 실사이트 3개의 gold는 `null`이며 정확도 계산에서 제외했다. Laya는 실제 공유 번들을 CPU에서 실행했고, Gemma는 E2B/v15 원문 ID 프롬프트를 사용했다. 프롬프트 튜닝과 target-v8 승격은 없었다.

| 실제 초기 경로 | 대상 수 | 모델 실행 |
|---|---:|---|
| LOW 이하로 후보 제외 | 3 | 없음 |
| 규칙 광고 확정 | 7 | 없음; 실사이트 3개 포함 |
| 규칙 정상 확정 | 1 | 없음 |
| 예방 문맥에 따른 Laya 생략 | 2 | 초기 실험에서는 모델 미실행; 후속 통합 결과는 2절 |
| 실제 Laya 경계 후보 | 11 | 중복 제거한 8개 입력으로 실제 배치 추론 |
| 위 11개 중 confidence < 0.8 | 0 | 초기 저신뢰 조건으로는 Gemma 미호출 |
| 위 11개의 정책외 Gemma 진단 | 11 | PoC용 순차 호출; 배포 호출로 계산하지 않음 |

Laya는 3종 gold(`ad/clean/uncertain`)와 **7/11**, Gemma 정책외 진단은 **8/11** 일치했다. Laya는 이진 모델이므로 정보부족 gold에 `uncertain`을 출력할 수 없다는 한계도 이 비교에 포함된다. 광고/정상 gold 9개에 한정하면 각각 7/9, 8/9다. 작은 수작업 표본의 결과이며 일반 정확도 추정치가 아니다.

| 대상 ID / 내용 | 고정 gold | Laya | Gemma 정책외 진단 |
|---|---|---|---|
| S08 교육 인용 `카지노`, S18 재활 문맥 `홈타이` | clean | ad: 오탐 | clean: 일치 |
| S09 가입 권유 `카지노`, S13 필로폰 판매, S16 현금 포커 | ad | ad: 일치 | ad: 일치 |
| S12 의약품 성분 설명, S14 식물 관찰, S15 포커 확률 | clean | clean: 일치 | clean: 일치 |
| S10 문맥 없는 `카지노`, S17 문맥 없는 `홈타이` | uncertain | ad: 불일치 | clean: 불일치 |
| S11 `비아그라 처방전 없이 판매합니다.` | ad | ad: 일치 | clean: 누락 |

Laya의 정상 문맥 오탐은 confidence 0.9501/0.9066으로 높았다. 저신뢰 조건만으로 이 오류들은 후속 검증에 들어가지 않는다. Gemma 진단은 이 오탐 2개를 수정했지만 S11에서는 정답을 오답으로 바꿨다. 배포에서는 confidence ≥0.8 bypass 정책을 유지한다.

규칙에서 끝난 사례도 확인해야 한다. S20 `출장마사지`는 gold가 정보부족인데 4점 규칙 광고였다. S21 숨김 교육 인용은 `TRANSPARENT` 후보에 예방 감점이 적용되지 않아 6점 규칙 광고였다. 실사이트 링크는 링크 묶음 +3점으로 각각 7/6/6점 규칙 광고였으며, 목적지의 불법성 정답은 확인하지 않았다. 이들은 초기 Gemma 진단 대상이 아니었다.

### 초기 S06/S07 입력 계약

| ID | 후보의 context 점수 근거 | 위치 | score / verdict / decided_by | gold |
|---|---|---|---|---|
| S06 | `[["page-context", -4]]` | `p` | `2 / borderline / rules` | clean |
| S07 | `[["body-context", -4]]` | `p:nth-of-type(1)` | `2 / borderline / rules` | clean |

[routes.json](poc/20261018/routes.json)에서 `id`에 해당하는 `target`을 기존 judge 객체로 가져온다. 둘 다 `ml_prob`와 confidence가 없다. 이를 0으로 채워 저신뢰 결과로 위장하지 않고 예방 문맥에 따른 미처리 예외로 연결한다.

Gemma 입력은 [inputs.json](poc/20261018/inputs.json)의 해당 행에서 가져온다. 아래는 초기 실험 당시의 v15 입력 계약이다. 실제 production 입력은 후속 실험에서 따로 기록했다.

```json
[
  {
    "candidate_id": "S06",
    "html": "<p id=\"target\">카지노 바카라</p>",
    "context": "도박 예방교육 자료의 용어 목록입니다."
  },
  {
    "candidate_id": "S07",
    "html": "<p id=\"target\">카지노 바카라</p>",
    "context": "예방 교육 본문의 인용 용어입니다."
  }
]
```

`target.context`는 **점수 근거 배열**, Gemma 입력의 `context`는 **원문 문맥 문자열**이다. 같은 필드명이라도 서로 대입하면 안 된다. gold는 평가에만 사용하고 모델 입력에 넣지 않는다. 위 JSON은 초기 입력 계약이며, 후속 production 실험에서 DOM 문맥과 binding이 달라진 내용은 2절에 기록했다.

### 초기 실험의 저장 증거 검증

저장소 루트에서 다음 명령을 실행한다. 표준 Python 라이브러리만 사용하며 파일 쓰기, 네트워크, 모델 호출은 없다.

```sh
python3 docs/poc/20261018/verify.py
```

검증기는 파일 해시, 24개 입력과 라벨의 연결, 실제 Laya 출력의 중복 매핑, Gemma 원 응답과 판정의 일치, evidence ID/원문 offset/source hash, 토큰 합계, 초기 저신뢰 0건, 초기 단계에서의 S06/S07 모델 미실행을 확인한다. 저장 응답에서 **24/11/7/8**을 다시 계산해 [measured-summary.json](poc/20261018/measured-summary.json)과 비교한다. 이는 저장된 증거의 재검산이며 모델의 재추론이나 독립적인 의미 정답 검증은 아니다.

| 파일 | 보존 내용 |
|---|---|
| [metadata.json](poc/20261018/metadata.json) | 기준 commit, 코드·모델·프롬프트 해시, 설정, 시간 순서, 초기/확정 정책, 제거 항목 |
| [inputs.json](poc/20261018/inputs.json) | 24개 원문 HTML·문맥·재생 document·추론 전 gold |
| [routes.json](poc/20261018/routes.json) | 원래 경로, 점수 상세, 실제 target 객체 |
| [laya.json](poc/20261018/laya.json) | 동일 QUESTION, SDK 버전, 8개 배치 원 응답, 11개 후보 매핑, 확률·confidence·usage·시간 |
| [gemma.json](poc/20261018/gemma.json) | 11개 정책외 진단의 실제 입력·원 응답·토큰·시간·원문 binding |
| [prompt-v15.txt](poc/20261018/prompt-v15.txt) | 실제 사용한 프롬프트 원문 |
| [saved-site-input.json](poc/20261018/saved-site-input.json) | 저장 실사이트 링크 3개와 관련 원문 조각 |
| [failures.json](poc/20261018/failures.json) | 모델 실행 전 추출 함수 호출 오류 및 AST 의존 함수 누락, 복구 기록 |
| [original-verification.json](poc/20261018/original-verification.json) | 원 실행 당시 소스·라벨 무변경 및 프로세스 종료 확인 |
| [export-audit.json](poc/20261018/export-audit.json), [checksums.json](poc/20261018/checksums.json) | 원본 보존 확인, 공개 산출물 무결성 |

동일 추론을 별도로 재현하려면 metadata의 해시에 맞는 모델과 코드가 필요하다. 규칙 코드는 기록된 Git commit을 사용하고, Laya는 기록된 QUESTION과 정규화 입력을 정렬·중복 제거해 CPU 2 threads, `predict_batch(batch_size=16)`으로 실행한 조건이다. SDK는 Laya 0.4.2 / torch 2.5.1 / transformers 4.57.6이다. Gemma는 E2B, runtime b11429, CPU 2 threads, context 4096, 병렬 슬롯 1, v15이며 세부 생성 설정은 metadata와 각 원 응답에 있다. 이 export에는 모델 가중치·서버 바이너리·별도 benchmark engine 구현을 포함하지 않으므로 자료만으로 모델 재실행이 가능한 패키지는 아니다. 최초 자료 내보내기 단계에서는 재추론하지 않았다. 이후 실험의 호출은 해당 절에 따로 기록했다.

### 초기 실험의 실행 범위와 한계

- 최신 commit의 `extract.js`로 로컬 HTML을 추출하고, main.py에서 원 함수·상수를 AST로 가져와 `candidates_from`과 `judge(use_ml=False)`를 실행했다. 이후 실제 Laya SDK로 동일 QUESTION/배치 계약을 실행했다. 전체 Python 모듈 import와 새로운 production client 통합 테스트는 당시 수행하지 않았다.
- 합성 문맥은 추론 전 별도 context 필드에 고정했다. 이후 동일 문맥을 HTML aside에 넣어 다시 추출해 대상 후보·점수·경로·모델 입력의 동일성을 확인했다. 이 후속 DOM 확인은 모델 재추론이 아니며, 이번 공개 검증기는 당시 확인 기록을 보존할 뿐 DOM을 재실행하지 않는다.
- 실사이트는 저장된 본문 두 조각을 한 HTML로 재구성했다. 라이브 전체 페이지 크롤링이 아니며 `page_url`은 같은 호스트의 재생용 URL이다.
- Laya 배치 실측은 약 0.269초다. Gemma 11건의 응답 usage 합계는 입력 **12,186**, 출력 **282** 토큰이다. 첫 호출과 후속 캐시 효과가 섞인 로컬 측정이므로 하드웨어 일반 성능으로 해석하지 않는다. Laya 원 기록의 `calls[].started_at`은 호출 직후 기록된 값이며 정확한 시작 시각이 아니다.
- Laya를 종료한 뒤 Gemma 서버 1개로 순차 실행했다. 두 단계 종료 코드와 프로세스 종료 확인을 보존했다. 모델 오류·누락·잘림은 없었으며, 실행 전 준비 오류 2건은 숨기지 않고 보존했다.
- 개인 절대경로, PID, 세션키 경로, 명령어 환경, 자격증명, 서명 URL은 내보내지 않았다. Gemma 원 응답의 `model` 필드는 절대경로 대신 파일명으로 바꿨다. 실제 출력 내용·usage·timings·binding은 보존했다. 공개 원문에 포함된 일반 링크는 원문 무결성을 위해 유지했다.
- 모델 가중치 사본과 전체 서버 로그는 제외했다. 초기 원본은 변경하지 않았다. 이 자료의 저장 증거 검증과 독립 품질 검토는 서로 구분한다.

## 2. 후속 production 통합 — 2026-10-18 18:57~19:01 KST

`detector.main.run`을 실제 모듈 import로 호출했다. loopback HTTP 서버의 `/S06.html`, `/S07.html`과 sitemap만 제공하고 실제 Chromium → 최신 `extract.js` → 후보·규칙 → production `analyze` → `GemmaClient` → report/result 저장까지 실행했다. fixture는 기존 frozen case에 동일 문맥 aside를 넣은 context-replay 문서다. AST subset, 가짜 모델 출력, 전수 강제 옵션은 사용하지 않았다.

설정은 `time_budget=120`, `llm_reserve=30`, concurrency 1, confidence 0.8, CPU 2 threads였다. app.py/bootstrap을 호출하지 않았다. 크롤링·모델 HTTP 요청은 loopback만 사용했고 외부 사이트를 방문하지 않았다. Git HEAD는 초기 commit과 같지만 production 변경사항은 작업 트리에 있으므로 **이번 코드 식별자는 HEAD와 별도로 기록한 파일별 SHA256**이다.

### 관측 오류 수정 후 확인된 결과

| 대상 | 초기 gold | 실제 규칙 | 대상 Laya | production Gemma | 저장 결과 |
|---|---|---|---|---|---|
| S06 | clean | 2점, page-context -4 | 예방 문맥으로 생략 | clean / completed | report에 정상, result findings 제외 |
| S07 | clean | 2점, page-context -4 | 예방 문맥으로 생략 | clean / completed | report에 정상, result findings 제외 |

잔여 DOM은 추가로 **12개**가 수집됐다. 실제 Laya 배치 1회로 12개 모두 정상/confidence ≥0.8로 선별했고 Gemma 잔여 추출은 **0회**였다. 대상 Gemma 검증은 **2회**, 입력/출력 usage 합계는 **2,537 / 57 토큰**이다. report는 `needs_review=false`, 미해결 0건, AI 실패 0건, 방문 2페이지, result findings 0건이었다. 실행 시간은 32.8초이며 하드웨어 일반 성능을 뜻하지 않는다.

### 입력·binding 차이와 검증 범위

대상 HTML `<p id="target">카지노 바카라</p>`는 기존 frozen 입력과 같다. 다만 production은 문맥을 DOM에서 직접 추출하므로 기존 수작업 context 문자열과 **바이트 단위로 동일한 모델 입력은 아니다**.

- S06 문맥은 대상 p와 인접 aside의 HTML이다. 근거는 실제 예방교육 문장에 결합됐다.
- S07 문맥은 대상 p와 바로 다음의 “제1장에서는 도박 중독 예방과 피해 지원…” p다. 뒤쪽 aside나 8개 본문 전체를 모델에 전달하지 않았다.
- 최신 `candidates_from`은 `source_near_text`에 page-context 계산을 우선 적용한다. 따라서 S07의 초기 `body-context` 근거가 이번에는 **page-context**로 바뀌었다. 두 사례의 예방 문맥 예외 연결은 확인했지만 **body-context 분기 자체를 별도로 실행했다고 주장하지 않는다**.
- `candidate_id`도 S06/S07 고정 ID에서 production의 URL/프레임 namespace + DOM owner + 검증 순번으로 바뀌었다. source ID에는 실제 출력의 반복 namespace도 그대로 보존했다. 새로운 source hash, evidence ID, offset, owner와 원문 일치를 검증했다.

### 첫 관측 실패와 복구 — 숨기지 않는 별도 기록

첫 실행의 관측기가 모든 Python 프레임의 `f_locals`를 읽어 Laya import에 영향을 줬다. `laya/lang.py:201`의 comprehension에서 다음 예외가 발생했다.

```text
NameError: cannot access free variable 'w' where it is not associated with a value in enclosing scope
```

추가 모델 호출 없이 import만 비교했을 때 관측기 없음은 성공, 기존 관측기는 실패, 필요한 함수만 읽는 수정 관측기는 성공했다. **production 소스 결함으로 귀속할 수 없는 PoC 관측 오류**다. production 소스를 수정하지 않고 관측기만 좁혀 동일 두 fixture를 재실행했다.

첫 실행에서도 두 대상은 clean이었지만 Laya 선별 실패 때문에 잔여 12개가 Gemma 추출로 넘어갔다. 그래서 첫 실행은 Gemma **14회(추출 12 + 대상 검증 2)**, 입력/출력 18,391 / 434 토큰이었고 Laya 실패 기록 12개가 남았다. 이를 정상 production 성능 근거로 합산하지 않는다. 실제 추가 추론 사용량을 숨기지 않기 위해 **두 실행 전체 Gemma 호출은 16회**로 기록한다. 이는 초기 24개 PoC의 정책외 진단 11회와 별개다.

두 실행 시작 시 production 파일 해시는 같았다. 재실행 도중 `app_paths.py` 파일 해시 변경을 관측했다. 확인된 차이는 호출하지 않은 `bootstrap()`의 환경변수 기본값 3줄 추가다. 실험에서는 해당 파일을 수정하지 않았으며 이미 import된 `main.run`을 실행했다. detector/local_llm 파일과 공유 Laya 번들의 해시는 유지됐다. [source-drift.json](poc/20261018/production-context/source-drift.json)에 전후 해시와 차이를 보존했다.

### 별도 증거 및 종료 확인

- [통합 단계 합계](poc/20261018/production-context/summary.json), [확인 실행 원시자료](poc/20261018/production-context/confirmed.json): 실제 report/result, Laya 원 응답, Gemma 원 응답·토큰·binding, 단계 관측, 코드 해시.
- [첫 관측 실패 원시자료](poc/20261018/production-context/attempt1-observer-failure.json), [import traceback 및 대조](poc/20261018/production-context/import-diagnostics.json): 개인 경로를 제거한 실제 오류 기록.
- [fixture 입력](poc/20261018/production-context/fixture-inputs.json): 두 페이지의 HTML과 기존 gold.
- Laya 객체가 해제된 후 Gemma 서버가 열렸음을 weak reference로 확인했다. 재실행에서 실제 Laya 배치 완료 후 서버 진입 시 살아 있는 Agent 객체는 0개였다. 동일 Python 프로세스에서 모델 객체를 순차 사용한 것이며 모델별 별도 Python 프로세스를 주장하지 않는다.
- Gemma 서버는 동시 최대 1개, 종료 코드 0이었다. 세션키 삭제, fixture 서버 종료, Chromium·Gemma·실행 프로세스와 자식 PID 부재를 확인했다. 강제 정리나 watchdog timeout은 없었다. 양쪽 실행의 프로세스가 모두 종료됐다.
- 초기 24개 원본과 내보낸 측정 데이터는 그대로 보존했다. 추가 실행 결과를 원래 **24/11/7/8**에 더하지 않았다. 모델 가중치·전체 서버 로그·개인 경로·세션키는 공개 자료에서 제외했다.

저장 증거만 재검증하려면 다음을 실행한다. 모델 추론은 하지 않는다.

```sh
python3 docs/poc/20261018/production-context/verify.py
python3 docs/poc/20261018/verify.py
```

## 3. 별도 저신뢰 probe — 2026-10-18 19:32 KST

최종 런타임의 자동 Gemma 호출 경로를 검증할 입력을 찾기 위해 한국어 문구 16개를 **추론 전에 한 번만 고정**했다. 초기 24개 평가와 별도 데이터이며 의미 gold는 모두 `null`이다. 결과를 보고 문구를 추가하거나 수정하지 않았다.

코드 기준은 `feature/windows-installer`의 `5ccf04c918ded2c5e9d042bc9d90f7664d1d45be`와 기록된 소스 해시다. 실제 Chromium에서 현재 `extract.js`를 실행하고 전체 모듈의 `candidates_from`/`judge(use_ml=False)`로 경로를 확인했다. 16개 모두 원래 규칙의 **1.5 < score < 4.0** 경계 후보였고 예방 문맥 우회는 없었다. 실제 Laya 0.4.2 / torch 2.5.1 / transformers 4.57.6, 동일 QUESTION, CPU 2 threads로 1회 배치 추론했다. 결과 누락·잘림·오류는 없었다.

| ID | 고정 문구 | 규칙 점수 | Laya Ptrue | confidence `max(p, 1-p)` | 후속 입력 선정 |
|---|---|---:|---:|---:|---|
| P03 | 홀덤 동호회 참가 문의 | 3.0 | 0.4854 | 0.5146 | 선정 |
| P06 | 유흥 관련 문의 | 2.0 | 0.4368 | 0.5632 | 원시자료에 보존 |
| P11 | 홈타이 예약 문의 | 3.0 | 0.4658 | 0.5342 | 선정 |

나머지 13개는 confidence ≥0.8이었다. 이 수치는 운영 분기용 실출력이며 탐지 정확도·정답 확률의 평가가 아니다. 이번 단계에서는 **Gemma 호출 0회, 전체 production scan 0회**다. Laya 객체와 실행 프로세스 종료를 확인했다. 입력·모델 번들·규칙 소스 해시는 실행 전후 일치했다.

P03/P11은 원문 HTML과 실제 추출 문맥이 각각 동일한 `<p id="target">…</p>`다. 원문 텍스트, selector, `source_id`, `source_sha256`, 정규화 입력, SDK 원 응답·usage·배치 시간이 보존돼 있다. 이 단계의 `source_id`는 페이지별 DOM owner이며, 향후 실제 HTTP scan에서는 URL/프레임 namespace가 붙어 달라질 수 있다.

probe 결과로는 **P03/P11 + 기존 교육 문맥 S06/S07**을 선정했다. 이후 잔여 회수 경로 관찰용 R01 하나를 추가해 최종 fixture를 5개로 고정했다. 추가 probe 탐색은 하지 않았다. 이 단계 당시에는 전체 scan을 실행하지 않았으며, 이후 실행 결과는 4절에 구분했다.

- [probe 합계](poc/20261018/laya-boundary-probe/summary.json), [고정 16개 입력](poc/20261018/laya-boundary-probe/inputs.frozen.json)
- [규칙·binding·실제 Laya 결과](poc/20261018/laya-boundary-probe/probe-results.json), [Laya 배치 원 응답](poc/20261018/laya-boundary-probe/laya.raw.json)
- [최초 선정 입력 4개 기록](poc/20261018/laya-boundary-probe/planned-final-inputs.json)
- [최종 고정 fixture 5개](poc/20261018/final-five-case/fixtures.frozen.json), [실행 전 고정 계획](poc/20261018/final-five-case/plan.json), [현재 실행 상태](poc/20261018/final-five-case/run-status.json)

추가한 R01의 대상 HTML은 다음과 같다.

```html
<p id="target">처방전 없이 수면제를 판매합니다</p>
```

R01은 규칙 점수 0으로 기존 후보에서 제외될 것으로 예상해 고정한 fixture다. 준비 단계에서는 점수나 모델 출력을 새로 측정하지 않았다. Laya가 고신뢰 clean으로 선별해 회수되지 않으면 미탐으로 기록하기로 했으며 임계값 조작, 모의 응답, 추가 후보 탐색은 하지 않았다. 실제 결과는 아래 4절에 기록했다.

저장 자료만 검증하며 추가 추론하지 않는 명령:

```sh
python3 docs/poc/20261018/laya-boundary-probe/verify.py
```


## 4. 고정 5개 production scan

### 이전 실행 — 4eaacf9, 2026-10-18 19:59 KST

실행 HEAD `4eaacf9`에서 실제 `main.run` → Chromium/extract.js → spawn Laya → 필요한 Gemma → report/result 저장을 한 번 수행했다. 핵심 코드 해시는 실행 전후 같고 `659567`과도 동일했다. 고정 입력 5개는 변경하지 않았다.

| 대상 | Laya Ptrue / confidence | 실제 경로 | 최종 판정 |
|---|---|---|---|
| P03 | 0.4855 / 0.5145 | 규칙 경계 → 저신뢰 → Gemma verify | clean |
| P11 | 0.4658 / 0.5342 | 규칙 경계 → 저신뢰 → Gemma verify | clean |
| S06 | 대상 Laya 생략 | 교육 문맥 예외 → Gemma verify | clean |
| S07 | 대상 Laya 생략 | 교육 문맥 예외 → Gemma verify | clean |
| R01 | 0.8722 / 0.8722 | 규칙 0점·후보 제외 → 잔여 Laya → Gemma extract → 재합류 → Laya 고신뢰 판정 재사용 | ad |

실제 Laya JSON IPC 배치 입력/응답은 16개이며, 5페이지의 잔여 문구를 포함한다. Gemma 호출은 **verify 4 + extract 1 = 5회**, 강제 진단은 0회다. R01은 태그 포함 HTML 근거로 회수돼 `result_extra.json`에 게시형 `ETC` 1건으로 저장됐다. `result.json`의 적격 4종 findings는 0건이다. 잔여 정상 선별 15건, AI 실패·미해결 0건, `needs_review=false`, scan 29.91초였다.

Laya 자식 PID 19253의 실제 ready/predict/ok 교환, 종료 및 Pipe 정리 후 Gemma PID 19255의 시작을 관측했다. 서버는 동시 최대 1개였고 전체 자식 프로세스와 세션키가 정리됐다. 실제 source binding과 원시 토큰·시간 검증을 통과했다. 이 실험은 경로 관찰이며 P03/P11의 의미 정답이나 이전 평가 정확도를 갱신하지 않는다.

이후 통지된 상위/하위 owner 오귀속 수정은 이번 실험의 실행 소스에 포함되지 않는다. 추가 사례나 모델 호출 없이 당시 소스와 실행 후 변경 관찰을 별도로 보존했다.

[이전 4eaacf9 실행 보고서](poc/20261018/final-five-case/REPORT-4eaacf9.md) · [실측 합계와 사례별 경로](poc/20261018/final-five-case/run-final/summary.json) · [실제 IPC·응답·PID](poc/20261018/final-five-case/run-final/observations.json) · [production report](poc/20261018/final-five-case/run-final/output/report.json)

### 최종 실행 — ef70919, 2026-10-18 20:17~20:18 KST

owner 구조 수정 후 같은 5개를 실제 production 경로로 한 번 실행했다. P03/P11은 Laya 저신뢰 후 Gemma clean, S06/S07은 교육 문맥 예외 후 Gemma clean, R01은 규칙 0점 잔여에서 Laya 0.8722 → Gemma 회수 → 재합류 후 ad였다. 실제 spawn 입력 16개, Gemma verify 4회·extract 1회이며 강제 진단은 없었다. 실패·미해결 0개, 추가 게시형 결과 1개, 실측 29.55초다.

Laya/Gemma 회수와 세션키 삭제를 확인했다. 실행 프로세스 종료 직후 남은 Python 자식 1개는 감독 코드가 추가 종료했으며, 최종적으로 관찰한 모든 PID가 사라졌다. 소스·입력 전후 hash 및 원문 binding 검증을 통과했다. 이전 24개 평가, 16개 probe, 이전 5개 scan 원시자료는 보존했다.

[최종 보고서](poc/20261018/final-five-case/FINAL_REPORT.md) · [최종 실측 합계](poc/20261018/final-five-case/run-ef70919/summary.json) · [최종 production report](poc/20261018/final-five-case/run-ef70919/output/report.json)
