"""고정 5개를 실제 production scan으로 한 번 실행하고 실제 IPC/응답을 관찰한다."""
import hashlib,importlib.metadata,json,os,shutil,subprocess,sys,threading,time,traceback
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[3]
WORKSPACE=REPO.parent
OUT=HERE/'run-final'
CORE_COMMIT='659567cccf9f5afdcf92200663ee8cb13462d126'
def now():return datetime.now(timezone.utc).isoformat()
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def read(p):return json.loads(p.read_text())
def clean(x):
    if isinstance(x,dict):return {k:clean(v) for k,v in x.items()}
    if isinstance(x,list):return [clean(v) for v in x]
    if isinstance(x,tuple):return [clean(v) for v in x]
    if isinstance(x,str):return x.replace(str(WORKSPACE),'<WORKSPACE>')
    return x
def save(n,x):
    dest=OUT/n;dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(clean(x),ensure_ascii=False,indent=2,default=str)+'\n')
def git(*args):return subprocess.check_output(['git','-C',str(REPO),*args],text=True).strip()

def execute():
    import psutil
    sys.path.insert(0,str(REPO))
    from detector.main import run
    from app_paths import laya_model,gemma_model,llama_server
    assert not [p.pid for p in psutil.process_iter(['name']) if 'llama-server' in (p.info['name'] or '')], 'existing model server'
    cases=read(HERE/'fixtures.frozen.json');plan=read(HERE/'plan.json')
    assert len(cases)==5 and sha(HERE/'fixtures.frozen.json')==plan['fixtures_sha256']
    paths=list((REPO/'detector').glob('*.py'))+list((REPO/'local_llm').glob('*.py'))+[REPO/'detector/extract.js',REPO/'app_paths.py',REPO/'training/make_dataset.py']
    before={str(p.relative_to(REPO)):sha(p) for p in paths}
    expected={n:hashlib.sha256(subprocess.check_output(['git','-C',str(REPO),'show',f'{CORE_COMMIT}:{n}'])).hexdigest() for n in before}
    assert before==expected,'core source differs from approved commit'
    bundle=laya_model();model_hashes={str(p.relative_to(bundle)):sha(p) for p in bundle.rglob('*') if p.is_file()}
    meta=dict(started_at=now(),actual_start_head=git('rev-parse','HEAD'),branch=git('branch','--show-current'),approved_core_commit=CORE_COMMIT,core_hashes_before=before,approved_core_hashes=expected,fixtures_sha256=plan['fixtures_sha256'],laya_model_hashes_before=model_hashes,gemma_model_sha256=sha(gemma_model()),llama_server_sha256=sha(llama_server()),versions={n:importlib.metadata.version(n) for n in ('laya','torch','transformers','playwright')},configuration=dict(time_budget=120,llm_reserve=30,concurrency=1,max_pages=0,timeout=15000,use_ml=True,laya_confidence_threshold=.8),observer='filtered call/return profile of production runtime and engine; no replacement functions; actual JSON IPC observed on parent I/O threads',stage='five_fixed_production_cases_only')
    save('metadata.json',meta)
    documents={f"/{c['id']}.html":c['document'] for c in cases}
    assert all('http://' not in s and 'https://' not in s and '<script' not in s for s in documents.values())
    events=[];ipc=[];model_calls=[];browser_requests=[];fixture_requests=[];laya_pids=[];gemma_pids=[];samples=[];stop=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            path=urlsplit(self.path).path;fixture_requests.append(dict(at=now(),path=path))
            if path=='/sitemap.xml':
                body=('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join(f'<url><loc>http://127.0.0.1:{self.server.server_port}{p}</loc></url>' for p in documents)+'</urlset>').encode();kind='application/xml';status=200
            elif path in documents:body=documents[path].encode();kind='text/html; charset=utf-8';status=200
            else:body=b'not found';kind='text/plain';status=404
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
    entry=f'http://127.0.0.1:{server.server_port}/P03.html';meta['entry_url']=entry
    def audit(name,args):
        if name=='socket.connect' and isinstance(args[1],tuple):
            if args[1][0] not in ('127.0.0.1','localhost','::1'):raise RuntimeError('non-loopback Python connection blocked')
    sys.addaudithook(audit)
    def monitor():
        while not stop.wait(.1):
            try:
                active=[]
                for p in psutil.Process().children(recursive=True):
                    try:
                        if 'llama-server' in p.name():active.append(p.pid)
                    except psutil.NoSuchProcess:pass
                samples.append(dict(at=now(),gemma_pids=active))
            except psutil.NoSuchProcess:break
    mon=threading.Thread(target=monitor,daemon=True);mon.start()
    timing={}
    def profile(frame,event,arg):
        file=frame.f_code.co_filename.replace('\\','/');name=frame.f_code.co_name
        runtime=file.endswith('/detector/laya_runtime.py') and name in ('__enter__','_exchange','_send_message','_receive_message','predict_batch','close')
        engine=file.endswith('/local_llm/engine.py') and name in ('__enter__','__exit__','infer')
        ai=file.endswith('/detector/ai.py') and name=='analyze'
        browser=file.endswith('/detector/main.py') and name=='secure_route'
        process_close=file.endswith('/multiprocessing/process.py') and name=='close'
        if not (runtime or engine or ai or browser or process_close):return
        loc=frame.f_locals
        try:
            if ai and event=='call':save('initial-rule-candidates.json',loc['cands']);save('extracted-pages.json',loc['pages'])
            if browser and event=='call':browser_requests.append(dict(url=loc['request'].url,resource_type=loc['request'].resource_type))
            if runtime:
                obj=loc.get('self');pid=obj.pid if obj else None
                if name=='_send_message' and event=='call':ipc.append(dict(at=now(),direction='parent_send',message=loc['message']))
                if name=='_receive_message' and event=='return' and isinstance(arg,dict):ipc.append(dict(at=now(),direction='parent_receive',message=arg))
                if name=='_exchange':
                    if event=='call':timing[id(frame)]=time.perf_counter();events.append(dict(event='laya_exchange_start',at=now(),pid=pid,request_op=(loc.get('request') or {}).get('op','load_ready'),child_alive=bool(obj._process and obj._process.is_alive())))
                    elif event=='return':events.append(dict(event='laya_exchange_end',at=now(),pid=pid,seconds=time.perf_counter()-timing.pop(id(frame),time.perf_counter()),response_status=arg.get('status') if isinstance(arg,dict) else None))
                if name=='__enter__' and event=='return' and arg is not None:
                    laya_pids.append(pid);events.append(dict(event='laya_ready',at=now(),pid=pid,alive=psutil.pid_exists(pid),start_method='spawn',test_target_is_none=obj._target is None))
                if name=='close' and event=='return' and pid:
                    events.append(dict(event='laya_close',at=now(),pid=pid,process_absent=not psutil.pid_exists(pid),process_handle_cleared=obj._process is None,pipe_closed=obj._conn is None,io_thread_cleared=obj._io_thread is None))
            if process_close and event=='call':
                obj=loc.get('self')
                if obj is not None and getattr(obj,'name','')=='laya-worker':events.append(dict(event='laya_process_close',at=now(),pid=obj.pid,exitcode=obj.exitcode))
            if engine:
                obj=loc.get('self')
                if name=='__enter__' and event=='call':events.append(dict(event='gemma_enter',at=now(),laya_pids_seen=list(laya_pids),laya_all_absent=bool(laya_pids) and all(not psutil.pid_exists(p) for p in laya_pids)))
                if name=='__enter__' and event=='return' and arg is not None:
                    gemma_pids.append(obj.proc.pid);events.append(dict(event='gemma_ready',at=now(),pid=obj.proc.pid,threads=obj.threads,startup_seconds=obj.startup_seconds))
                if name=='__exit__' and event=='return':events.append(dict(event='gemma_exit',at=now(),pid=obj.proc.pid if obj.proc else None,returncode=obj.proc.poll() if obj.proc else None,key_removed=not getattr(obj,'key_file',OUT/'absent').exists()))
                if name=='infer' and event=='return' and isinstance(arg,dict) and arg.get('task')=='verify':model_calls.append(arg)
        except Exception:events.append(dict(event='observation_error',traceback=traceback.format_exc()))
    threading.setprofile(profile);sys.setprofile(profile)
    status='error'
    try:
        report=run(entry,OUT/'output',**meta['configuration'],phase=lambda phase:events.append(dict(event='phase',phase=phase,at=now())))
        status='completed';print('scan completed',report['meta']['elapsed_sec'],flush=True)
    except BaseException:save('failure.json',dict(at=now(),traceback=traceback.format_exc()));raise
    finally:
        sys.setprofile(None);threading.setprofile(None);server.shutdown();server.server_close();server_thread.join(3);stop.set();mon.join(3)
        meta.update(status=status,finished_at=now(),actual_finish_head=git('rev-parse','HEAD'),core_hashes_after={str(p.relative_to(REPO)):sha(p) for p in paths},fixtures_sha256_after=sha(HERE/'fixtures.frozen.json'),laya_model_hashes_after={str(p.relative_to(bundle)):sha(p) for p in bundle.rglob('*') if p.is_file()})
        save('metadata.json',meta);save('observations.json',dict(events=events,ipc=ipc,gemma_calls=model_calls,browser_requests=browser_requests,fixture_requests=fixture_requests,laya_pids=laya_pids,gemma_pids=gemma_pids,max_observed_gemma_servers=max((len(s['gemma_pids']) for s in samples),default=0),samples=samples,laya_processes_absent=all(not psutil.pid_exists(p) for p in laya_pids),gemma_processes_absent=all(not psutil.pid_exists(p) for p in gemma_pids),remaining_children=[dict(pid=p.pid,name=p.name()) for p in psutil.Process().children(recursive=True)]))
        originals={}
        for p in (OUT/'output').glob('*.json'):
            originals[p.name]=sha(p);save('output/'+p.name,read(p))
        save('output-redaction.json',dict(pre_redaction_file_hashes=originals,transformation='workspace absolute path prefix replaced by <WORKSPACE>; content/usage/binding unchanged'))

def launch():
    import psutil
    assert not OUT.exists(),'single final run already exists; do not rerun'
    OUT.mkdir();temp=OUT/'.runtime';temp.mkdir()
    for name in ('tmp','appdata','cache'):(temp/name).mkdir()
    env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','PLAYWRIGHT_BROWSERS_PATH':'0','PYTHONDONTWRITEBYTECODE':'1','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2','TOKENIZERS_PARALLELISM':'false','TMPDIR':str(temp/'tmp'),'LOCALAPPDATA':str(temp/'appdata'),'HF_HOME':str(temp/'cache/hf'),'TORCH_HOME':str(temp/'cache/torch'),'XDG_CACHE_HOME':str(temp/'cache'),'NO_PROXY':'127.0.0.1,localhost,::1','AD_PURIFIER_MODEL':str(WORKSPACE/'build_review/20261017/laya-model-smoke'),'AD_PURIFIER_GEMMA_MODEL':str(WORKSPACE/'gemma_benchmark/.runtime/models/gemma-4-E2B_q4_0-it.gguf'),'AD_PURIFIER_LLAMA_SERVER':str(WORKSPACE/'gemma_benchmark/.runtime/bin/llama-b11429/llama-server')}
    logfile=temp/'execution.log';stream=logfile.open('w');start=now();p=subprocess.Popen([str(REPO/'.venv/bin/python'),str(Path(__file__).resolve()),'execute'],env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
    known={};t=time.monotonic();timedout=False
    while p.poll() is None:
        try:
            for child in psutil.Process(p.pid).children(recursive=True):known[child.pid]=child.create_time()
        except psutil.NoSuchProcess:pass
        if time.monotonic()-t>170:timedout=True;p.terminate();break
        try:p.wait(timeout=.2)
        except subprocess.TimeoutExpired:pass
    try:p.wait(timeout=10)
    except subprocess.TimeoutExpired:p.kill();p.wait()
    stream.close();save('execution-log.json',dict(text=logfile.read_text()))
    alive=[]
    for pid,created in known.items():
        try:
            child=psutil.Process(pid)
            if child.create_time()==created and child.is_running():alive.append(child)
        except psutil.NoSuchProcess:pass
    forced=[dict(pid=c.pid,name=c.name()) for c in alive]
    for child in alive:child.terminate()
    _,remaining=psutil.wait_procs(alive,timeout=3)
    for child in remaining:child.kill()
    psutil.wait_procs(remaining,timeout=3)
    save('lifecycle.json',dict(started_at=start,finished_at=now(),execution_pid=p.pid,exit_code=p.returncode,watchdog_timeout=timedout,forced_cleanup=forced,execution_process_absent=not psutil.pid_exists(p.pid),observed_child_pids=list(known),all_observed_children_absent=all(not psutil.pid_exists(pid) for pid in known)))
    shutil.rmtree(temp)
    print('final scan process exit',p.returncode,'timeout',timedout,flush=True)
    sys.exit(p.returncode or (1 if timedout else 0))

if __name__=='__main__':execute() if len(sys.argv)>1 and sys.argv[1]=='execute' else launch()
