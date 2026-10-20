"""저장된 production 통합 관찰만 검증한다. 모델 실행/네트워크/파일 쓰기 없음."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
def read(n):return json.loads((ROOT/n).read_text())
def digest(b):return hashlib.sha256(b).hexdigest()
for n,h in read('checksums.json').items():assert digest((ROOT/n).read_bytes())==h,n
summary=read('summary.json'); confirmed=read('confirmed.json'); failed=read('attempt1-observer-failure.json')
for record in (confirmed,failed):
    r=record['report'];o=record['observations'];s=record['summary']
    assert len(r['candidates'])==2 and r['pages']==2
    assert r['ai']['mode']=='policy' and r['ai']['laya_confidence_threshold']==.8
    assert record['result']['findings']==[]
    leaves=[]
    for c in r['candidates']:
        assert c['score']==2 and c['verdict']=='clean' and c['decided_by']=='gemma'
        assert 'ml_prob' not in c
        assert c['context']==[['page-context',-4]]
        call=c['gemma']['results'][0]['call'];inp=call['input'];p=call['parsed'];leaves.append(call)
        assert p['owner_id']==inp['candidate_id']
        assert inp[p['source_field']][p['start_char']:p['end_char']]==p['evidence']
        assert p['source_sha256']==digest(json.dumps({k:inp[k] for k in ('html','context')},ensure_ascii=False,sort_keys=True).encode())
        assert json.loads(call['raw_response']['choices'][0]['message']['content'])==call['model_output']
        assert call['model_output']['verdict']==p['verdict']=='clean'
    for outer in r['ai'].get('extraction',{}).get('calls',[]):leaves.extend(outer['block_calls'])
    assert len(leaves)==s['gemma_actual_inference_requests']
    assert sum(c['raw_response']['usage']['prompt_tokens'] for c in leaves)==s['gemma_prompt_tokens']
    assert sum(c['raw_response']['usage']['completion_tokens'] for c in leaves)==s['gemma_completion_tokens']
    assert o['max_observed_servers']==1 and o['server_processes_absent'] and not o['remaining_children']
    assert record['launcher']['worker_absent'] and record['launcher']['tracked_children_absent']
    assert record['launcher']['worker_exit_code']==0 and not record['launcher']['forced_child_cleanup']
    assert all(e['laya_objects_alive']==0 for e in o['events'] if e['event']=='gemma_server_enter')
    assert all(e['key_removed'] and e['returncode']==0 for e in o['events'] if e['event']=='gemma_server_exit')
    assert record['metadata']['laya_model_hashes']==record['metadata']['laya_model_hashes_after']
assert confirmed['report']['ai']['failures']==[]
assert (confirmed['report']['ai']['laya_inputs'],confirmed['report']['ai']['residual_screened_clean'],confirmed['report']['ai']['gemma_verify'],confirmed['report']['ai']['gemma_extract'])==(12,12,2,0)
calls=confirmed['observations']['laya_calls'];assert len(calls)==1 and len(calls[0]['states'])==len(calls[0]['outputs'])==12
assert '카지노 바카라' not in calls[0]['states']
for output in calls[0]['outputs']:
    answer=output['answers']['is_illegal_ad'];assert answer['noul']<.5 and answer['confidence']>=.8
    assert not output['usage']['truncated'] and output['usage']['state_tokens_dropped']==0
assert failed['summary']['ai_failure_records']==12 and not failed['observations']['laya_calls']
assert [r['exit_code'] for r in read('import-diagnostics.json')['paired_no_model_import_checks']]==[0,1]
assert read('import-diagnostics.json')['corrected_observer_import_check']['status']=='IMPORT_OK'
assert summary['total_integration_stage_gemma_requests_including_failed_observation']==16
assert summary['body_context_specific_branch_independently_exercised'] is False
assert confirmed['summary']['production_source_changed_during_run']==['app_paths.py']
print(json.dumps({'status':'PASS stored-evidence checks','confirmed_targets':2,'confirmed_gemma_verify':2,'confirmed_gemma_extract':0,'confirmed_residual_laya':12,'failed_attempt_gemma_requests':14,'total_gemma_requests':16,'body_context_branch_separately_verified':False,'concurrent_file_change':'app_paths.py bootstrap only; documented'},ensure_ascii=False,indent=2))
