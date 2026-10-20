# 고정 5개 production PoC — 2026-10-18 19:59 KST

**한 번의 실제 scan을 완료했다.** 시작·종료 HEAD는 `4eaacf99a04366f72ab2933cba40ab55b7f5640e`이고, 핵심 소스는 승인된 `659567cccf9f5afdcf92200663ee8cb13462d126`과 파일별 해시가 같았다. 실행 중 소스·고정 입력·Laya 번들 해시는 유지됐다. 배포 소스 매핑은 라이선스 보완 커밋 `4eaacf9`이다.

실행 완료 뒤 통지된 **상위 직접 텍스트/하위 광고 owner 오귀속 수정은 이 실험에 포함되지 않는다.** 실행 당시 동일 해시였다는 기록을 이후 변경 코드의 검증으로 확대하지 않는다. 추가 모델 호출이나 사례 확장은 하지 않았다.

보고서 작성 시점에는 실제로 `detector/ai.py`의 실행 후 해시 변경을 확인했다. HEAD는 여전히 `4eaacf9`였으며 변경된 작업 트리는 재실행하지 않았다. [작성 시점 소스 상태](run-final/source-status-at-report.json)에 측정 해시와 구분해 보존했다.

## 실제 경로

| ID | 대상 | 규칙 점수 | 실제 Laya Ptrue / confidence | 실제 경로 | 최종 판정 |
|---|---|---:|---|---|---|
| P03 | 홀덤 동호회 참가 문의 | 3 | 0.4855 / 0.5145 | 경계 → Laya 저신뢰 → Gemma verify | clean |
| P11 | 홈타이 예약 문의 | 3 | 0.4658 / 0.5342 | 경계 → Laya 저신뢰 → Gemma verify | clean |
| S06 | 교육 인용 카지노 바카라 | 2 | 대상 Laya 생략 | page-context 예외 → Gemma verify | clean |
| S07 | 본문 교육 인용 카지노 바카라 | 2 | 대상 Laya 생략 | page-context 예외 → Gemma verify | clean |
| R01 | 처방전 없이 수면제를 판매합니다 | 0 | 0.8722 / 0.8722 | 규칙 후보 제외 → 잔여 Laya → Gemma extract → 재합류 → 기존 Laya 고신뢰 판정 재사용 | ad |

R01은 초기 후보 목록에 없었다. Gemma가 **태그를 포함한 대상 HTML 전체**를 광고 근거로 반환했고, production이 같은 원문 owner와 본문 일치를 확인해 `recovered_without_rule_hit=true`로 재합류시켰다. 재합류 후 confidence ≥0.8이므로 별도 Gemma verify는 호출하지 않았다. 고신뢰 광고 잔여의 extract와 후보의 verify bypass는 서로 다른 운영 단계다. 강제 진단 호출은 없다.

S07은 최신 인접 문맥 계산에 따라 이번에도 `page-context`였다. `body-context` 분기를 별도로 검증했다고 주장하지 않는다. P03/P11의 의미 gold는 없으므로 clean이 정답이라고 평가하지 않는다. P03의 Ptrue는 앞선 probe의 0.4854에서 이번 배치에서는 0.4855였고 저신뢰 경로는 같았다.

## 호출·출력·회수 증거

- 실제 `detector.main.run` import와 loopback HTTP fixture 5페이지, Chromium, 현재 `extract.js`, 규칙, `LayaRunner`, `GemmaClient`를 사용했다. 입력 원문은 [fixtures.frozen.json](fixtures.frozen.json)과 동일하다. 제한은 `time_budget=120`, `llm_reserve=30`, CPU 2 threads, concurrency 1, confidence 0.8이다.
- Laya는 **spawn 자식 PID 19253**에서 실행됐다. 실제 JSON IPC `ready → predict → ok`와 출력 16개를 보존했다. 고정 5페이지에서 자연스럽게 수집된 잔여 문구가 함께 포함된 수치이며 새 사례를 생성한 것이 아니다. Laya 입력/출력 토큰은 1,134 / 0이다.
- Gemma는 **검증 4회 + 회수 1회 = 실제 추론 5회**였다. 입력/출력 usage 합계는 6,042 / 139 토큰이며, 서버 PID는 19255였다. 잔여 정상 선별 15건, AI 실패 0건, 미해결 0건, `needs_review=false`였다.
- `result.json`의 적격 4종 findings는 0건, `result_extra.json`은 R01의 게시형 `ETC` 1건이다. 후보별 최종 판정은 `report.json`에 있다. scan 실측은 29.91초이며 로컬 CPU 측정이다.
- Laya 응답 후 production의 terminate/join으로 자식을 종료했다(exit code -15). PID 부재·Pipe 닫힘·통신 스레드 정리를 확인한 **뒤** Gemma가 시작됐다. 정상 종료 방식인 -15를 모델 오류로 해석하지 않는다.
- Gemma 서버 동시 최대 1개, 종료 코드 0, 세션키 삭제를 확인했다. 실행 프로세스와 관찰한 모든 자식 PID가 종료됐으며 watchdog timeout·추가 강제 정리는 없었다.
- 모델 응답의 원문 owner, evidence ID, source hash, offset, 실제 문자열 일치를 재검산했다. 원시 응답의 개인 경로만 `<WORKSPACE>`로 치환했고 출력·토큰·binding·PID는 보존했다. 실제 환경변수와 세션키·경로, 전체 서버 로그는 공개하지 않았다.

## 이전 측정과 구분

| 실험 | 측정 목적 | Laya | Gemma | 결과 해석 |
|---|---|---|---|---|
| 초기 24개 | 규칙·의미 평가 | 11개 후보 | 정책외 진단 11회 | 기존 7/11, 8/11 일치 수치 유지 |
| 별도 16개 probe | 저신뢰 경로 입력 확보 | 16개 | 0회 | 저신뢰 3개 발견; 정확도 평가 아님 |
| 이번 고정 5개 | 실제 production 경로 | spawn 배치 1회, 입력 16개 | verify 4 + extract 1 | 사례별 경로와 저장·회수 관찰; 이전 정확도에 합산하지 않음 |

앞선 교육 2개 통합 실험의 정상 실행 및 관측 오류 기록도 그대로 보존했다. 이번 실험은 로컬 실행이며 Windows CI 실행 `39121162936`의 성공 여부를 대신하지 않는다.

## 원시자료

- [합계·사례별 경로](run-final/summary.json), [실행 완료 상태](run-status.json)
- [report](run-final/output/report.json), [result](run-final/output/result.json), [result_extra](run-final/output/result_extra.json)
- [실제 IPC·모델 응답·PID·시간](run-final/observations.json), [프로세스 종료](run-final/lifecycle.json)
- [실행 전후 소스·모델·입력 해시](run-final/metadata.json), [실행 직후 소스 상태](run-final/post-run-source-status.json), [보고서 작성 시점 소스 변경](run-final/source-status-at-report.json)
- [최초 규칙 후보](run-final/initial-rule-candidates.json), [실제 DOM 추출](run-final/extracted-pages.json)

`analyze_saved.py`는 저장된 응답·binding·호출 수를 재검산해 summary를 작성하는 도구이며 추론하지 않는다. 원시자료 검증은 통과했다. 추가 실험은 수행하지 않았다.
