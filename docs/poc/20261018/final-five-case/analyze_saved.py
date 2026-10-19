"""실행 완료 원시자료를 재검산한다. 모델/브라우저/네트워크 실행 없음."""
import hashlib,json
from pathlib import Path
from urllib.parse import urlsplit

HERE=Path(__file__).resolve().parent;OUT=HERE/'run-final'
def read(n):return json.loads((OUT/n).read_text())
def digest(value):return hashlib.sha256(value).hexdigest()
cases=json.loads((HERE/'fixtures.frozen.json').read_text());m=read('metadata.json');o=read('observations.json');r=read('output/report.json');life=read('lifecycle.json');initial=read('initial-rule-candidates.json')
assert digest((HERE/'fixtures.frozen.json').read_bytes())==m['fixtures_sha256']==m['fixtures_sha256_after']
assert m['core_hashes_before']==m['core_hashes_after']==m['approved_core_hashes']
assert m['laya_model_hashes_before']==m['laya_model_hashes_after']
assert m['status']=='completed' and life['exit_code']==0 and not life['watchdog_timeout']
assert not life['forced_cleanup'] and life['all_observed_children_absent'] and life['execution_process_absent']
assert not [e for e in o['events'] if e['event']=='observation_error']
assert len(o['laya_pids'])==len(o['gemma_pids'])==1 and o['max_observed_gemma_servers']==1
ready=[x for x in o['ipc'] if x['direction']=='parent_receive' and x['message'].get('status')=='ready']
send=[x for x in o['ipc'] if x['direction']=='parent_send'];receive=[x for x in o['ipc'] if x['direction']=='parent_receive' and x['message'].get('status')=='ok']
assert len(ready)==len(send)==len(receive)==1
request=send[0]['message'];response=receive[0]['message'];assert request['op']=='predict'
assert len(request['texts'])==len(response['outputs'])==r['ai']['laya_inputs']==16
for out in response['outputs']:
    assert not out['usage']['truncated'] and out['usage']['state_tokens_dropped']==0 and not out['usage']['truncated_questions']
close=next(e for e in o['events'] if e['event']=='laya_close');enter=next(e for e in o['events'] if e['event']=='gemma_enter')
assert close['process_absent'] and close['pipe_closed'] and close['io_thread_cleared'] and close['at']<enter['at'] and enter['laya_all_absent']
assert next(e for e in o['events'] if e['event']=='laya_ready')['test_target_is_none']
assert next(e for e in o['events'] if e['event']=='gemma_exit')['key_removed']
assert len(o['gemma_calls'])==5 and sum(c['request_count'] for c in o['gemma_calls'])==5
for call in o['gemma_calls']:
    p=call['parsed'];inp=call['input'];raw=call['raw_response']
    assert call['status']=='ok' and not call['validation']['errors']
    assert raw['choices'][0]['finish_reason']=='stop'
    assert json.loads(raw['choices'][0]['message']['content'])==call['model_output']
    assert call['model_output']['verdict']==p['verdict'] and call['model_output']['evidence_id']==p['evidence_id']
    assert p['owner_id']==inp['candidate_id'] and inp[p['source_field']][p['start_char']:p['end_char']]==p['evidence']
    assert p['source_sha256']==digest(json.dumps({k:inp[k] for k in ('html','context')},ensure_ascii=False,sort_keys=True).encode())
assert r['pages']==5 and not r['crawl_incomplete'] and r['ai']['failures']==[]
assert all(urlsplit(x['url']).hostname=='127.0.0.1' for x in o['browser_requests'])
assert not {q['path'] for q in o['fixture_requests']} - {f"/{c['id']}.html" for c in cases} - {'/sitemap.xml','/favicon.ico'}
case_rows=[]
for c in cases:
    cid=c['id'];final=next(x for x in r['candidates'] if Path(urlsplit(x['url']).path).stem==cid and x['evidence_text']==c['text'])
    original=next((x for x in initial if Path(urlsplit(x['url']).path).stem==cid and x['evidence_text']==c['text']),None)
    assert final['source_html']==c['html']
    predictions=[out for state,out in zip(request['texts'],response['outputs']) if state==c['text']]
    assert len(predictions)<=1
    p=predictions[0]['answers']['is_illegal_ad']['noul'] if predictions else None
    confidence=max(p,1-p) if p is not None else None
    extracted=final.get('extraction');verify=final.get('gemma')
    if extracted:
        assert extracted['source_field']=='html' and final['recovered_without_rule_hit']
        assert extracted['evidence']==final['source_html']
        route='규칙 후보 제외 → Laya 잔여 선별 → Gemma 회수 → 재합류 → 기존 Laya 고신뢰 판정 재사용'
    elif p is None:route='교육 문맥 경계 → Laya 생략 → Gemma 검증'
    else:
        assert confidence<.8
        route='규칙 경계 → Laya 저신뢰 → Gemma 검증'
    case_rows.append(dict(id=cid,text=c['text'],gold=c['gold'],initial_rule_candidate=original is not None,rule_score=original['score'] if original else final['score'],route=route,p_true=p,confidence=confidence,verdict=final['verdict'],decided_by=final['decided_by'],gemma_verify_called=verify is not None,gemma_extract_called=extracted is not None,source_id=final['source_id'],source_sha256=final['source_sha256'],location=final['location'],binding_verified=True))
assert [x['verdict'] for x in case_rows]==['clean','clean','clean','clean','ad']
assert case_rows[-1]['initial_rule_candidate'] is False and case_rows[-1]['rule_score']==0
assert r['ai']['gemma_verify']==4 and r['ai']['gemma_extract']==1 and r['ai']['residual_screened_clean']==15
assert read('output/result.json')['findings']==[]
extra=read('output/result_extra.json')['findings'];assert len(extra)==1 and extra[0]['evidence_text']==cases[-1]['text'] and extra[0]['technique']=='ETC'
summary=dict(stage='fixed_five_case_production',actual_start_head=m['actual_start_head'],actual_finish_head=m['actual_finish_head'],binary_source_mapping='4eaacf99a04366f72ab2933cba40ab55b7f5640e',approved_core_commit=m['approved_core_commit'],core_source_unchanged_during_run=True,original_fixtures_unchanged=True,fixture_count=5,case_rows=case_rows,laya_input_count=16,laya_batches=1,gemma_verify=4,gemma_extract=1,gemma_inference_requests=5,gemma_prompt_tokens=sum(x['raw_response']['usage']['prompt_tokens'] for x in o['gemma_calls']),gemma_completion_tokens=sum(x['raw_response']['usage']['completion_tokens'] for x in o['gemma_calls']),laya_input_tokens=sum(x['usage']['input_tokens'] for x in response['outputs']),laya_output_tokens=sum(x['usage']['output_tokens'] for x in response['outputs']),residual_screened_clean=15,needs_review=r['needs_review'],unresolved_count=r['unresolved_count'],ai_failures=r['ai']['failures'],core_findings=0,extra_findings=1,elapsed_seconds=r['meta']['elapsed_sec'],laya_pid=o['laya_pids'][0],gemma_pid=o['gemma_pids'][0],laya_close_before_gemma=True,processes_terminated=True,maximum_gemma_servers=1,forced_diagnostic_calls=0,observation_status='stored raw evidence checks passed',quality_evaluation=False,new_parent_child_owner_fix_not_exercised=True)
(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
status=dict(status='completed_for_recorded_commit',result_directory='run-final',summary='run-final/summary.json',report='run-final/output/report.json',model_observations='run-final/observations.json',future_source_fix_status='上位owner/childowner 수정 통지 이후 추가 실행 없음'.replace('上位','상위'),additional_runs=0)
(HERE/'run-status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in summary.items() if k!='case_rows'},ensure_ascii=False,indent=2))
