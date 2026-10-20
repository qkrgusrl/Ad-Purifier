# 통합 테스트 인계 계약

trace: windows-installer-20261018 / 사용자 최신 수정 반영.
검증 기준 HEAD: `e6cd33168c7544019c603af05c789c6d4bed2183`, branch `feature/windows-installer`.
이 worker는 fetch하지 않았다. 다른 worker가 최신 main을 반영하면 아래 명령으로 재검증한다.

## 실행

저장소 루트에서:

```sh
PYTHONPATH=tests .venv/bin/python -B -m unittest -v test_ai_integration test_source_capture
```

Python 표준 unittest만 필요하다. Playwright, Laya, Gemma 패키지·브라우저·모델 가중치 없이 실행 가능하다.
기존 코드에서 36 tests / 33 assertion failures / 3 pass / 0 errors. 종료 코드 1은 의도한 RED다.
`ai_red_output.txt`는 위 명령의 원본 출력이며 `ai_red_results.json`은 사례별 구조화 결과다.

## 최신 동작 범위

- LOW=1.5, HIGH=4.0 및 기존 judge의 룰 선별을 유지한다. 기존 확정 rule ad/clean은 Laya/Gemma를 호출하지 않는다.
- Laya는 기존 borderline과 잔여 단위를 선별하며 예방 page/body-context 우회도 유지한다. 우회되어 유효 확률이 없는 borderline은 review로 남긴다.
- Gemma는 유효 Laya confidence < 0.8인 단위만 검증·추출한다. 오류·누락·잘림은 Gemma로 대체하지 않고 review로 남긴다. confidence는 max(p, 1-p)이며 실제 정확도 보장이 아니다.
- 공개 설정 seam 제안: `run(..., laya_confidence_threshold=0.8)`. 이름을 바꾸려면 테스트 작성자와 합의한다. 규칙 임계값은 바꾸지 않는다.
- --no-ml/use_ml=False는 명시적 진단 모드다. 애매한 경계는 uncertain + review로 남기고 ai.mode=diagnostic을 기록한다.
- 잔여는 기존 후보가 사용한 source_id만 제외한다. 문구가 같다는 이유로 다른 DOM 소유 단위를 제외하지 않는다.
- 잔여 단위별 Laya screening 후 고신뢰 정상은 제외하고, 고신뢰 광고는 기존 candidates_from으로 위치·기법을 회수한다. 회수 불가 시 원문과 Laya 판정을 보존해 review로 남긴다. 저신뢰만 Gemma.extract로 보내며 회수 후에도 유효 저신뢰 조건을 만족해야 Gemma.verify를 호출한다. 동일 원문 Laya 캐시 재사용을 허용한다.
- 회수 뒤 룰 ad가 되면 verify를 호출하지 않는다. 재추출과 반복 회수는 source 단위로 유한해야 한다.

## 수집 더블과 소유권

`OfflineRun`은 main.crawl만 AsyncMock으로 바꾼다. main.run, candidates_from, judge, dedupe, 결과 저장은 실제 코드다.
원문 보존 테스트 4개는 실제 candidates_from을 직접 호출하며 나머지는 실제 run을 호출한다.

crawl 반환은 기존 `(pages, issues)`다. 페이지는 `(url, [(frame_prefix, data)])`이며 기존 hidden/texts/links/head에 `data.source_blocks` 목록을 추가한다.
source_blocks의 각 항목과 대응 hidden/texts 항목은 다음 필드를 공유한다.

| 필드 | 계약 |
|---|---|
| source_id | 수집된 DOM 소유 단위 ID. 같은 원문의 여러 기법은 같은 ID; 다른 소유 단위는 다른 ID |
| source_html | 소유 단위의 수집 당시 원본 HTML. evidence_text에서 재합성 금지 |
| source_context | 주변 원문 문자열. 기존 context 점수 가중치 목록과 별도 |
| selector | DOM 수집기가 정한 위치. frame_prefix는 기존 방식으로 붙임 |
| text / raw | 기존 수집기의 표시 텍스트 / 정규화 전 텍스트 |
| signals | 기존 결정적 기법 신호. 모델은 selector/location/technique를 결정하지 않음 |
| tag / cls / href | 기존 수집 계약 유지 |

fixture에서는 source ID를 s1/s2/s3처럼 전역 고유하게 준다. 실제 수집기의 페이지/프레임별 ID 생성 규칙은 이 테스트가 지정하지 않는다.
`candidate_ids=[]`는 source_blocks에는 남았지만 기존 후보 수집에서 누락된 단위를 만든다. 실제 extract.js의 DOM 실행을 대체하는 검증은 아니다.
같은 문단의 a 세 개는 서로 다른 selector/source_id/html과 같은 문단 context를 가진다.
기존 제출 dedupe가 합친 경우 report.duplicates에도 source_id를 남겨 DOM 소유권을 추적한다.

## 모델 더블과 bridge

Laya 주입: `laya.agent.Agent(...).predict_batch(texts, QUESTION, batch_size=...)`.
응답은 기존 `{'answers': {'is_illegal_ad': {'noul': p}}}`를 사용한다. 잘림 seam은 항목의 `truncated=True`이며 오류와 짧은 배치도 별도 테스트한다.

Gemma 주입: `local_llm.client.GemmaClient`, 동기 context manager.
최종 계약은 부모가 확정한 원본 prototype 보존이다. 초기 목록/flat 응답은 부모의 제안이었으며 실제 사용자가 API 자료형을 지정한 것이 아니다.
`tests/test_gemma_protocol.py`와 구현 B가 사용하는 계약과 동일하게 맞췄다.

```python
verify({'candidate_id': str, 'html': str, 'context': str})
# -> {'task': 'verify', 'version': 'v15', 'status': 'ok' | 'needs_review',
#     'results': [{'candidate_id': str, 'status': str,
#                  'verdict': 'ad' | 'clean' | 'uncertain', 'needs_review': bool,
#                  'evidence': str, 'owner_id': str, 'evidence_id': str,
#                  'source_field': str, 'start_char': int, 'end_char': int,
#                  'source_sha256': str, 'evidence_scope': str, 'call': dict}]}

extract({'blocks': [{'block_id': str, 'html': str, 'context': str}, ...]})
# -> {'task': 'extract', 'version': 'v15', 'status': 'ok' | 'needs_review',
#     'input_block_count': int,
#     'candidates': [{'block_id': str, 'evidence': str, 'owner_id': str,
#                     'evidence_id': str, 'source_field': str,
#                     'start_char': int, 'end_char': int, 'source_sha256': str}, ...],
#     'failures': [{'block_ids': [str, ...], 'error': str, 'action': str}, ...],
#     'calls': [dict, ...], 'next_step': str}
```

binding은 별도 중첩 dict가 아니라 verify results 항목 / extract candidates 항목에 평탄하게 둔다.
잔여 block_id는 수집 source_id를 사용한다. verify candidate_id는 구현이 정하며 더블은 전달받은 ID에 binding을 맞춘다.
source_sha256은 `{html, context}`를 ensure_ascii=False, sort_keys=True로 JSON 직렬화한 UTF-8의 SHA-256이다. v15의 해당 계약만 참고했고 target-v8은 가져오지 않았다.

fixture의 bound_reply는 run_verify envelope를 반환한다. recover/partial/forged 등 extract callback은 `{'blocks': [...]}`를 받고 run_extract envelope를 반환한다.
extract_requests는 wrapper를 포함한 요청 기록이며 extract_inputs는 기존 소유 단위 assertion을 위한 평탄한 block 기록이다. 의미 assertion과 negative case는 유지한다.
local_llm 패키지 stub의 __path__는 실제 local_llm 디렉터리를 가리키므로 구현 예정인 engine/prompts/prototype import를 가리지 않는다. GemmaClient만 더블로 주입한다.

모델은 본문의 의미와 근거만 반환한다. 임의 selector/technique 필드는 무시하거나 응답 전체를 review로 처리할 수 있다. 위조 block_id를 후보로 만들거나 verify에 넘기면 실패한다.
명시적 clean은 `calls[].block_calls`의 해당 block_id와 성공한 `parsed.verdict == 'clean'`에서 추적한다. 후보가 없다는 사실만으로 미처리로 판단하지 않는다.
반면 candidates/failures/calls가 모두 빈 응답은 여전히 missing 출력이다. failures[].block_ids에 속한 항목과 verify의 누락/타 소유 candidate_id는 정상으로 확정하지 않는다.

## 보고서·마감·취소

report 최상위: needs_review(bool), unresolved_count(int), ai(dict).
후보: verdict=ad/clean/uncertain, processing_status, reason, source_id/source_html/source_context.
완료 processing_status는 completed이며 uncertain에는 완료와 다른 비어 있지 않은 상태와 이유가 필요하다.
미처리 잔여는 후보를 억지로 만들지 않아도 되지만 ai에 해당 source_id와 이유를 남기고 unresolved_count에 포함한다.
status=needs_review에 verdict=clean이 섞여도 uncertain으로 보존한다.
result.json은 meta/findings 및 기존 4종 기법 스키마를 유지하며 보류 항목은 report에만 남긴다.

더블 시계는 main.time.perf_counter를 제어한다. 크롤링 후/판정 중 마감 이후에는 새 모델 호출을 시작하지 않는다.
취소 시 ScanCancelled, client 종료, 기존 결과 파일 보존을 요구한다.
네트워크 연결과 subprocess.Popen을 막고 가중치 경로는 존재하지 않는 문자열로 준다.

## 검증 한계

이 테스트는 수집 결과 이후 원문 전달·판정·보고서 통합의 RED다. 실제 브라우저에서 extract.js가 원문 단위를 올바르게 수집하는지, Windows 설치/실행, 실모델의 탐지 성능은 검증하지 않았다.
별도 PoC worker의 실모델 결과와 이 테스트의 더블 결과를 혼동하면 안 된다.
향후 detector.ai.analyze를 만들어도 이 테스트는 해당 함수를 대체하지 않는다. 공개 run 경로로 실행한다.

## 각 테스트의 기존 코드 결과

PASS는 기존 동작 보존 테스트다. RED는 아래 assertion이 깨지는 것이 이번 실행의 기대 결과이며, 구현 완료 후에는 모두 통과해야 한다.

| 테스트 | 결과 | 관찰한 실패/보존 근거 |
|---|---|---|
| `test_borderline_low_confidence_reaches_gemma_and_can_reverse_laya` | RED | 0 != 1 : 실제 run에서 낮은 confidence가 Gemma에 도달하지 않음 |
| `test_cancel_before_judgment_preserves_existing_files` | PASS | 기존 동작 보존 |
| `test_cancel_during_gemma_closes_client_and_does_not_save` | RED | ScanCancelled not raised |
| `test_completed_report_distinguishes_clean_from_review` | RED | None is not False |
| `test_confident_borderline_uses_laya_without_gemma` | PASS | 기존 동작 보존 |
| `test_deadline_after_laya_does_not_start_gemma` | RED | 'ad' != 'uncertain' - ad + uncertain  |
| `test_deadline_marks_unprocessed_borderline_review_without_model_calls` | RED | None is not True : 미처리/보류가 보고서에서 정상으로 표시됨 |
| `test_educational_borderline_skips_laya_but_reaches_gemma` | RED | 0 != 1 : 교육 문맥 경계 후보도 Gemma 검증 필요 |
| `test_empty_extract_response_does_not_silently_clear_suspicious_residual` | RED | None is not True : 미처리/보류가 보고서에서 정상으로 표시됨 |
| `test_empty_verify_results_are_unprocessed_not_clean` | RED | 'clean' != 'uncertain' - clean + uncertain  |
| `test_forged_recovered_id_is_rejected_and_never_verified` | RED | None is not True : 미처리/보류가 보고서에서 정상으로 표시됨 |
| `test_gemma_error_keeps_uncertain_and_out_of_result` | RED | 'ad' != 'uncertain' - ad + uncertain  |
| `test_laya_error_is_verified_by_gemma` | RED | 0 != 1 : Laya 오류 후보가 검증 없이 누락됨 |
| `test_low_confidence_clean_is_uncertain_when_gemma_abstains` | RED | 'clean' != 'uncertain' - clean + uncertain  : Laya 0.45를 검증 없이 clean으로 확정하면 안 됨 |
| `test_no_ml_is_explicit_diagnostic_not_clean_certification` | RED | 'borderline' != 'uncertain' - borderline + uncertain  |
| `test_partial_extract_reports_unresolved_owner` | RED | None is not True : 미처리/보류가 보고서에서 정상으로 표시됨 |
| `test_recovered_model_cannot_overwrite_selector_or_technique` | RED | None is not true : 회수 결과를 조용히 버리면 안 됨 |
| `test_recovered_rule_ad_stays_rule_ad_without_gemma_verify` | RED | 0 != 1 |
| `test_repeated_recovery_of_same_source_is_finite` | RED | 0 != 1 |
| `test_residual_deadline_keeps_owner_unresolved` | RED | None is not True : 미처리/보류가 보고서에서 정상으로 표시됨 |
| `test_residual_excludes_used_source_id_only_not_duplicate_text` | RED | Lists differ: [] != ['s2']  Second list contains 1 additional elements. First extra element 0: 's2'  - [] + ['s2'] : 문구가 같아도 다른 소유 단위를 잔여에서 제외하면 안 됨 |
| `test_residual_high_confidence_clean_does_not_call_gemma` | RED | 0 != 1 : 잔여 원문은 Laya screening이 필요 |
| `test_residual_three_anchors_rejoin_judge_and_gemma` | RED | 0 != 3 : 한 문단의 잔여 a 세 개가 회수되지 않음 |
| `test_result_retains_four_techniques_and_excludes_uncertain` | RED | 3 != 2 |
| `test_review_status_cannot_be_overridden_by_clean_verdict` | RED | 'ad' != 'uncertain' - ad + uncertain  |
| `test_rule_extremes_stay_rules_and_call_neither_model` | PASS | 기존 동작 보존 |
| `test_short_laya_batch_verifies_each_unreturned_item` | RED | 0 != 1 : zip으로 짧은 배치 출력의 나머지를 버리면 안 됨 |
| `test_threshold_is_configurable_without_changing_rule_thresholds` | RED | 공개 run에 laya_confidence_threshold 설정 seam이 없음 |
| `test_truncated_laya_output_must_not_be_confident` | RED | 0 != 1 : 잘린 Laya 입력/출력의 0.99를 확정하면 안 됨 |
| `test_verify_result_cannot_bind_to_foreign_candidate_id` | RED | 'clean' != 'uncertain' - clean + uncertain  |
| `test_candidate_keeps_raw_html_context_and_owner` | RED | None != 's1' : source_id 원문 소실 |
| `test_context_string_is_not_score_weight_list_and_reaches_bridge_verbatim` | RED | 0 != 1 : Gemma 미호출 |
| `test_duplicate_text_retains_distinct_dom_owners_before_submission_dedupe` | RED | Items in the first set but not the second: None Items in the second set but not the first: 's3' 's2' 's1' |
| `test_iframe_prefix_preserves_selector_and_original_html` | RED | None != '<a href="/offer/s1">환전 안내</a>' |
| `test_missing_original_html_is_reviewed_not_synthesized` | RED | None is not True |
| `test_one_owner_with_two_techniques_keeps_shared_source_identity` | RED | Items in the first set but not the second: None Items in the second set but not the first: 's1' |
