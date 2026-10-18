# 최종 고정 5개 production PoC — ef70919

**2026-10-18 20:17:33~20:18:02 KST에 한 번 실행 완료했다.** 실행 시작·종료 HEAD는 `ef7091925e6c7eaea93e6fb144c2363f23a5b27e`이며 owner 구조 수정이 포함돼 있다. 고정 입력과 핵심 소스·Laya 모델 번들의 실행 전후 해시가 일치한다. 실제 `main.run`/Chromium/extract.js/LayaRunner/GemmaClient를 사용했고 임계값 0.8, 입력 5개, 운영 정책을 유지했다.

## 이번 실제 결과

| 사례 | Laya Ptrue / confidence | 관찰 경로 | 최종 판정 |
|---|---|---|---|
| P03 | 0.4855 / 0.5145 | 규칙 경계 → 저신뢰 → Gemma verify | clean |
| P11 | 0.4658 / 0.5342 | 규칙 경계 → 저신뢰 → Gemma verify | clean |
| S06 | 대상 Laya 생략 | 교육 문맥 예외 → Gemma verify | clean |
| S07 | 대상 Laya 생략 | 교육 문맥 예외 → Gemma verify | clean |
| R01 | 0.8722 / 0.8722 | 규칙 0점·후보 제외 → 잔여 Laya → Gemma extract → 재합류 → Laya 판정 재사용 | ad |

Laya spawn 배치 1회에 16개 입력이 포함됐다. 이는 고정 5페이지의 잔여 문구를 포함한 실제 입력이며 추가 fixture를 만든 것이 아니다. 실제 IPC의 ready/predict/ok와 출력 16개를 보존했다. Gemma는 **검증 4회·회수 1회**, 강제 진단 0회다. R01의 태그 포함 HTML 근거가 같은 원문 owner에 결합됐고 `recovered_without_rule_hit=true`로 재합류했다. confidence ≥0.8이므로 재합류 후 Gemma verify는 생략됐다.

- scan 실측 **29.55초**. Laya 입력/출력 토큰 1,134 / 0, Gemma 6,046 / 139.
- 잔여 정상 선별 15개, AI 실패·미해결 0개, `needs_review=false`.
- `result.json` 적격 4종 findings 0개, `result_extra.json` 게시형 R01 `ETC` 1개.
- P03/P11의 의미 gold는 없으므로 clean을 정답으로 평가하지 않는다. S07은 실제 page-context 경로였으며 body-context를 따로 시험하지 않았다. 요청 범위 밖의 중첩 owner 반례도 추가하지 않았다.

## 프로세스 회수

Laya 자식 **PID 21364**의 실제 IPC를 관측했다. production terminate/join으로 종료(exit code -15)한 뒤 PID 부재·Pipe·I/O 스레드 정리를 확인하고 Gemma **PID 21369**가 시작됐다. Gemma는 동시 최대 1개, 종료 코드 0이고 세션키가 삭제됐다.

실행 프로세스 종료 직후 Python 자식 **PID 21363** 하나가 남아 감독 코드가 추가 종료했다. 이 프로세스의 세부 역할은 수집 자료만으로 확정하지 않는다. 이를 자동 정리만으로 끝났다고 기록하지 않았다. 최종적으로 관찰한 모든 자식 PID와 실행 프로세스가 사라졌으며 watchdog timeout은 없었다.

## 이전 실험과 분리

| 실험 | 결과 |
|---|---|
| 초기 24개 평가 | 기존 24/11/7/8 측정값 보존. Gemma 11회는 정책외 진단 |
| 별도 16개 probe | 저신뢰 3개 발견, Gemma 0회. 정확도 평가 아님 |
| 이전 4eaacf9의 고정 5개 scan | Gemma verify 4 + extract 1, R01 ad, 29.91초. [이전 보고서](REPORT-4eaacf9.md)와 run-final 원시자료 보존 |
| 이번 ef70919의 같은 5개 scan | 경로·판정 동일, verify 4 + extract 1, 29.55초. 이번 커밋에서 scan 1회 |

실험 간 정확도를 합산하지 않는다. 로컬 CPU 결과이며 Windows 동일 소스 재빌드/CI 성공을 대신하지 않는다. 이번 원시자료 검증 후 추가 실험은 수행하지 않았다.

## 실제 저장 파일

- [실행 상태](run-status.json), [합계·사례별 경로](run-ef70919/summary.json)
- [production report](run-ef70919/output/report.json), [result](run-ef70919/output/result.json), [result_extra](run-ef70919/output/result_extra.json)
- [실제 IPC·모델 응답·PID·시간](run-ef70919/observations.json), [종료·추가 정리 기록](run-ef70919/lifecycle.json)
- [전후 해시](run-ef70919/metadata.json), [실제 추출](run-ef70919/extracted-pages.json), [초기 규칙 후보](run-ef70919/initial-rule-candidates.json)

`analyze_ef70919.py`는 저장 자료의 hash/binding/원 응답/합계를 재검산하며 추론하지 않는다. 원문 owner·ID·hash·offset·문자열 검증을 통과했다. 개인 절대경로와 실행 환경·세션키는 공개 자료에서 제외하고 실제 응답·토큰·시간·PID는 유지했다.
