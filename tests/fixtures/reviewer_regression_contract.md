# 추가 회귀 테스트 인계

## 실행과 증거

```sh
PYTHONPATH=tests .venv/bin/python -B -m unittest -v test_ai_integration test_source_capture
```

최신 원본 실행 출력: `reviewer_regression_output.txt`.
초기 RED 관찰과 최신 결과 요약: `reviewer_regression_results.json`.

첫 관찰은 45 tests / 12 assertion failures / 0 errors였다. 이 중 legacy 보고서 테스트 하나가 6개 subtest 실패를 냈다.
이후 병렬 생산 수정이 반영된 워크트리에서 추가 2개 테스트까지 실행한 결과는 47 tests / 4 assertion failures / 0 errors다.
최신 실패는 context 추출의 위치 결합 2개 subtest와 검토 키 충돌 2개 subtest다. 나머지 45개 테스트는 통과했다.
초기 12개 실패의 완전한 원본 로그는 보존하지 못했으며, 도구 실행에서 관찰한 assertion과 개수를 JSON에 별도로 기록했다. 최신 로그를 초기 RED 로그로 제시하지 않는다.
사용자가 제공한 기준 커밋은 703c2cc다. 이 작업은 Git 명령을 실행하지 않았으므로 최신 커밋/브랜치를 확인하거나 변경하지 않았다. 생산 파일도 수정하지 않았다.

## Windows fixture 경계

참조한 실패 로그는 저장소 상위의 `build_review/20261018/windows-run-39118381905-failure.log`다.
로그에는 ProactorEventLoop → socket.socketpair → _fallback_socketpair → connect → 네트워크 금지 assertion이 기록돼 있다.

OfflineRun은 기본 정책의 진짜 이벤트 루프를 socket guard 전에 생성한다. main 모듈에만 asyncio namespace 복사본을 주입하여 run을 loop.run_until_complete에 연결한다.
전역 asyncio.run과 event-loop policy는 변경하지 않는다. 기존 connect/create_connection 차단은 유지하며 ExitStack으로 loop를 닫는다.
공개 main.run은 실제로 실행하고 crawl coroutine만 더블로 교체한다. 모델 프로세스 실행도 차단한다.
새 테스트는 외부 주소에 대한 두 연결 API의 차단, 전역 정책/실행기 보존, loop 종료를 확인한다.
실행 환경은 macOS다. Windows Proactor에서의 최종 성공은 다음 Windows CI가 확인해야 한다.

## 실제 DOM 반례

reviewer_dom.capture는 현재 detector/extract.js를 실제 Chromium에서 실행한다. 로컬 HTML 문자열만 사용하고 HTTP 요청과 WebSocket을 차단한다.
Playwright 패키지 안에 설치된 브라우저가 있으면 PLAYWRIGHT_BROWSERS_PATH=0을 사용한다. 브라우저를 내려받거나 설치 실패를 skip으로 바꾸지 않는다.

| 테스트 | 요구 및 초기 반례 |
|---|---|
| test_reviewer_dom_body_title_aria_keep_distinct_evidence | 같은 요소의 본문/title/aria-label 3문구를 구분한다. 초기에는 aria-label과 첫 본문 evidence를 조합한 1개 후보만 남았다. |
| test_reviewer_dom_candidate_raw_and_attr_belong_to_its_evidence | evidence/raw/attr가 같은 문구를 가리켜야 한다. 초기 첫 본문에 마지막 속성이 덮어써졌다. |
| test_reviewer_dom_attribute_residual_not_discarded_with_used_body | 본문 후보가 사용한 DOM owner라도 미처리 속성 문구를 잃으면 안 된다. 초기 보고서에는 본문만 남았다. |
| test_reviewer_visible_zero_score_recovery_rejoins_as_posted | 실제 DOM의 '처방전 없이 수면제를 판매합니다'는 현재 규칙 0점이다. 원문 결합 회수 뒤 결정적 POSTED/원래 selector로 재합류하고 판정해야 한다. 초기 추출 호출은 있었으나 후보 수가 0이었다. |
| test_reviewer_detect_posted_false_also_applies_to_residual_recovery | 잔여 회수에도 게시형 비활성화를 적용하되 기존 은닉 탐지는 보존한다. 초기에는 POSTED가 다시 생겼다. |
| test_reviewer_legacy_history_restores_review_for_unfinished_work | 이전 보고서의 borderline/failed_pages/left_in_queue를 load_run과 list_runs에서 보수적으로 복원한다. 초기 상세는 needs_review 누락, 목록은 False였다. 조회 시 원본 보고서는 덮어쓰지 않는다. |
| test_reviewer_explicit_relative_model_path_is_relative_to_app_root | run(model='models/custom')가 다른 cwd에서도 app_root/models/custom을 runner에 전달해야 한다. 초기에는 상대 문자열을 그대로 전달했다. |

## 추가 위치 결합과 검토 상태

- test_reviewer_extract_context_only_evidence_cannot_create_owner_ad: source 본문은 경계 문구 '환전 안내'이며 광고 근거는 context에만 존재한다. owner/hash/span이 유효해도 source_field=context인 추출 근거로 해당 요소 위치에 광고를 만들면 안 된다. needs_review를 유지한다. 같은 테스트에서 예방 context에 결합된 verify clean은 허용되는지 별도로 확인한다.
- test_reviewer_same_element_review_keys_isolate_evidence_and_keep_legacy_review: 같은 요소의 3문구는 서로 다른 cand_key를 가져야 한다. 하나의 상태/메모를 저장한 뒤 이력 조회 시 다른 문구는 new/빈 메모를 유지해야 한다. source_id가 없는 이전 보고서는 기존 url|location|technique 해시로 저장된 상태를 그대로 읽거나 명시적으로 fallback해야 한다.

## LayaRunner 주입 계약

```python
LayaRunner(model_path, *, deadline=None, cancel=None, target=None)
with runner:
    runner.predict_batch(texts, questions, batch_size=16)
```

fixture는 detector.ai.LayaRunner를 모듈 수준에서 직접 패치하며 detector.laya_runtime import 경로도 더블로 제공한다.
predict callback은 기존 Agent의 dict list를 그대로 반환한다. answers/usage를 변환하지 않고 CancelledError/TimeoutError/RuntimeError도 삼키지 않는다.
Runner context 진입/종료 횟수를 기록하고 종료 전 Gemma 진입을 assertion으로 막는다. model_paths는 명시적 경로 전달을 관찰한다.
현행 생산 코드의 Agent 호출을 대상으로 먼저 기록한 RED를 유지하기 위해 과도기 Agent 더블도 남겼다. 실제 child에 sys.modules mock을 전달한다고 가정하지 않는다.
실제 spawn/child 재사용/JSON IPC/용량 제한/송수신 스레드 종료/PID 확인은 별도 runtime 독립 테스트 범위다. 이 통합 더블로 해당 보장을 입증했다고 주장하지 않는다.

기존 confidence 정책은 유지한다. 정상 완료 Laya confidence >= 0.8은 Gemma를 우회하며 운영 전수 재검토 모드는 추가하지 않는다.
