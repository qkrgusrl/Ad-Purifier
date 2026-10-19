"""고정 probe와 저장 Laya 응답 검증. 추론/네트워크/파일 쓰기 없음."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def read(n):return json.loads((ROOT/n).read_text())
def sha(b):return hashlib.sha256(b).hexdigest()
for n,h in read('checksums.json').items():assert sha((ROOT/n).read_bytes())==h,n
cases=read('inputs.frozen.json');rows=read('probe-results.json');raw=read('laya.raw.json');meta=read('manifest.json')
assert len(cases)==len(rows)==len(raw['states'])==len(raw['outputs'])==16
assert all(c['gold'] is None for c in cases)
assert sha((ROOT/'inputs.frozen.json').read_bytes())==meta['inputs_sha256']
assert meta['frozen_at']<raw['started_at']<raw['inference_started_at']
assert meta['source_hashes']==raw['source_hashes_after']
assert raw['model_hashes']==raw['model_hashes_after'] and raw['agent_object_released']
low=[]
for c,r,state,out in zip(cases,rows,raw['states'],raw['outputs']):
 assert c['id']==r['id'] and c['text']==r['text']==state
 t=r['target'];assert r['eligible'] and 1.5<t['score']<4 and t['verdict']=='borderline'
 assert not any(k in ('page-context','body-context') for k,w in t.get('context',[]))
 assert t['source_html']==c['html'] and t['normalized']==state
 value={'html':t['source_html'],'context':t['source_context']}
 assert t['source_sha256']==sha(json.dumps(value,ensure_ascii=False,sort_keys=True).encode())
 assert r['laya_output']==out and r['p_true']==out['answers']['is_illegal_ad']['noul']
 confidence=max(r['p_true'],1-r['p_true']);assert r['routing_confidence']==confidence
 assert not out['usage']['truncated'] and out['usage']['state_tokens_dropped']==0
 assert r['low_confidence']==(confidence<.8)
 if confidence<.8:low.append(r['id'])
assert low==read('summary.json')['low_confidence_ids']==['P03','P06','P11']
assert read('lifecycle.json')['process_absent'] and read('lifecycle.json')['exit_code']==0
assert read('summary.json')['gemma_calls']==0 and not read('summary.json')['production_runner_executed']
assert read('planned-final-inputs.json')['probe_selection_ids']==['P03','P11']
print(json.dumps({'status':'PASS stored probe checks','fixed_inputs':16,'actual_rule_borderline':16,'low_confidence_ids':low,'selected_for_future':['P03','P11'],'gemma_calls':0,'fullproduction_run':False,'semantic_accuracy_not_evaluated':True},ensure_ascii=False,indent=2))
