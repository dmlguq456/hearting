"""부모 비모델 보강: 실제 namespace와 공용 API, 임시 root만 변경. 생산 소스 변경 없음."""
import os,sys,json,subprocess,tempfile,select,hashlib,importlib.util,time,ctypes,unittest,shutil
from pathlib import Path
from unittest import mock
SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'utilities'))
import governor_identity as G
import dispatch_contract as D
from split_module_fixture import patch_split
s=importlib.util.spec_from_file_location('gov',SOURCE/'utilities/model-worker-governor.py');gov=importlib.util.module_from_spec(s);s.loader.exec_module(gov)
SCRIPT=Path(__file__).resolve()
def snap(root):
 p=Path(root)/'state.json';return json.loads(p.read_text()) if p.exists() else {}
def ident(pid=None):
 pid=pid or os.getpid();row={'pid':pid}
 try:
  raw=Path(f'/proc/{pid}/stat').read_text();tail=raw[raw.rfind(')')+2:].split();row.update(start=tail[19],state=tail[0],pgid=tail[2],namespace=os.readlink(f'/proc/{pid}/ns/pid'))
  status=Path(f'/proc/{pid}/status').read_text();row['NSpid']=next((x.split()[1:] for x in status.splitlines() if x.startswith('NSpid:')),[])
 except OSError as e:row.update(errno=e.errno)
 return row
def line(p):
 if not select.select([p.stdout],[],[],15)[0]:raise RuntimeError('fixture-response-timeout')
 raw=p.stdout.readline()
 if not raw:raise RuntimeError('fixture-exit:'+str(p.poll()))
 return json.loads(raw)
def finish_local_lease(root, token):
 # The actor already owns this real issued lease/FD. Reuse only its acquisition
 # result so gov.main runs the production child-wait/drain loop unchanged.
 # Two deterministic incomplete observations prove retention, rather than
 # increasing retries/timeouts until a noisy procfs scan happens to pass.
 scans=[]; transitions=[]
 original_scan=gov.process_group_observation; original_release=gov.release
 def scan(pgid):
  actual=original_scan(pgid)
  injected=len(scans)<2
  observed=actual._replace(reason="fixture-procfs-churn") if injected else actual
  scans.append({'actual':actual._asdict(),'observed':observed._asdict(),'injected_incomplete':injected})
  return observed
 def acquire_existing(requested_root, worker_class):
  assert Path(requested_root)==root and worker_class=='dispatch'
  assert token in snap(root)['leases']
  return token
 def release_observed(requested_root, requested_token):
  assert Path(requested_root)==root and requested_token==token
  handle=gov._LEASE_WITNESSES[gov._witness_key(root,token)]
  before=snap(root); witness_held=G.retained_witness_is_held(handle)
  first_scan=len(scans); result=original_release(root,token); after=snap(root)
  transitions.append({'observer':ident(),'witness_held':witness_held,'before':before,
                      'result':result,'after':after,'scans':scans[first_scan:]})
  return result
 argv=[str(SOURCE/'utilities/model-worker-governor.py'),'--root',str(root),'run','--class','dispatch','--',sys.executable,'-c','pass']
 assert gov.RESERVATION_ENV not in os.environ
 with mock.patch.object(gov,'acquire',acquire_existing), mock.patch.object(gov,'release',release_observed), patch_split(gov,'process_group_observation',scan), mock.patch.object(sys,'argv',argv):
  exit_code=gov.main()
 return {'root':str(root),'token':token,'exit':exit_code,'result':transitions[-1]['result'],
         'state':snap(root),'transitions':transitions,'acquisition_seam':'existing-actor-issued-lease'}
def local_drain_proven(returned):
 steps=returned['transitions']; token=returned['token']
 if len(steps)<3 or returned['exit']!=0:return False
 for step in steps[:-1]:
  result=step['result']
  if not (result.get('status')=='blocked' and result.get('occupied') is True
          and result.get('release_proven') is False
          and result.get('reason') in {'group-observation-incomplete','group-descendants-live'}
          and step['witness_held'] and token in step['after']['leases']
          and step['before']['leases']==step['after']['leases']):return False
 if not all(steps[i]['scans'][0]['injected_incomplete'] for i in (0,1)):return False
 last=steps[-1]; result=last['result']; scans=last['scans']
 if len(scans)!=1 or scans[0]['injected_incomplete']:return False
 observed=scans[0]['observed']; pid=last['observer']['pid']
 return (last['witness_held'] and not observed['reason'] and observed['state']!='unverifiable'
         and any(p==pid and state!='Z' for p,state in observed['members'])
         and all(p==pid or state=='Z' for p,state in observed['members'])
         and result=={'status':'released','release_proven':True,'occupied':False}
         and token not in last['after']['leases'] and not returned['state']['leases'])
def actor():
 root=Path(sys.argv[2]);assert root.name.startswith('governor-supplement-') and str(root).startswith('/tmp/')
 if os.getpgrp()!=os.getpid():os.setsid()
 handles={}; children=[]; leases=[]; print(json.dumps(ident()),flush=True)
 try:
  for raw in sys.stdin:
   r=json.loads(raw);cmd=r['cmd'];out={'observer':ident()};d=root/r.get('case','actor')
   if cmd=='quit':break
   if cmd=='hold':
    c=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.read()'],stdin=subprocess.PIPE);children.append(c)
    token=gov.acquire(d,'dispatch');leases.append((d,token));out.update(token=token,child=ident(c.pid),state=snap(d))
   elif cmd=='release':
    before=snap(d);lease=before.get('leases',{}).get(r['token'],{});out['before']=before
    out['recorded_pid_observed']=ident(lease.get('pid'));out['group_observation']=gov.process_group_observation(int(lease.get('pgid',-1)))._asdict()
    gov.release(d,r['token']);out['after']=snap(d)
   elif cmd=='witness':
    h=G.create_witness(d,'reservation');handles[r['case']]=h;out['binding']=h.binding()
   elif cmd=='observe':out['observation']=G.observe_witness(d,r['binding']).__dict__
   elif cmd=='finish':
    for c in children:c.stdin.close();c.wait(timeout=10)
    children.clear();out['returns']=[finish_local_lease(rd,t) for rd,t in leases]
   elif cmd=='ping':out['children']=[ident(c.pid) for c in children]
   print(json.dumps(out),flush=True)
 finally:
  for c in children:c.stdin.close();c.wait(timeout=10)
  for h in handles.values():G.close_witness(h)
class Actor:
 def __init__(self,inside,root,label):
  self.label=label;cmd=[sys.executable,str(SCRIPT),'actor',str(root)]
  if inside:cmd=['bwrap','--ro-bind','/','/','--bind',str(root),str(root),'--unshare-pid','--proc','/proc','--die-with-parent','--',*cmd]
  self.cmd=cmd;self.p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=not inside);self.id=line(self.p)
 def call(self,**r):self.p.stdin.write(json.dumps(r)+'\n');self.p.stdin.flush();return line(self.p)
 def close(self):
  self.p.stdin.write('{"cmd":"quit"}\n');self.p.stdin.flush();out,err=self.p.communicate(timeout=15);return {'exit':self.p.returncode,'stdout':out,'stderr':err}
def run():
 root=Path(tempfile.mkdtemp(prefix='governor-supplement-'));rows=[];actors=[]
 def record(name,expected,observed,raw):
  row={'case':name,'expected':expected,'observed':observed,'pass':expected==observed,'raw':raw};rows.append(row);(root/'raw.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps({k:v for k,v in row.items() if k!='raw'}),flush=True)
 # Collect orphan fixture children only; no daemon or operational process access.
 assert ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)==0
 try:
  outer=Actor(False,root,'outer');actors.append(outer);inner=Actor(True,root,'inner');actors.append(inner)
  sibling=Actor(True,root,'sibling');actors.append(sibling)
  for name,owner,observer in [('outer-owner-inner-observer',outer,inner),('inner-owner-outer-observer',inner,outer),('sibling',inner,sibling)]:
   w=owner.call(cmd='witness',case=name);ob=observer.call(cmd='observe',case=name,binding=w['binding']);alive=owner.call(cmd='ping')
   record('witness-'+name,{'state':'live','different_namespace':True},{'state':ob['observation']['state'],'different_namespace':w['observer']['namespace']!=ob['observer']['namespace']},{'owner':w,'observer':ob,'after':alive})
  for name,owner,observer in [('same-namespace-live-descendant',outer,outer),('foreign-invisible-release',outer,inner),('foreign-numeric-pid-release',inner,outer)]:
   held=owner.call(cmd='hold',case=name);rel=observer.call(cmd='release',case=name,token=held['token']);alive=owner.call(cmd='ping')
   record(name,{'lease_retained':True,'owner_alive':True,'descendant_alive':True},{'lease_retained':held['token'] in rel['after']['leases'],'owner_alive':'errno' not in alive['observer'],'descendant_alive':all('errno' not in x and x.get('state')!='Z' for x in alive['children'])},{'hold':held,'release':rel,'owner_after':alive})
  for a in (outer,inner):
   result=a.call(cmd='finish');record('local-return-'+a.label,True,all(local_drain_proven(x) for x in result['returns']),result)
  d=root/'wrapper-cancel';before=len(os.listdir('/proc/self/fd'));t,_=D.reserve_governor_token(SOURCE/'utilities/model-worker-governor.py',d,'dispatch');h=D._GOVERNOR_WITNESS_HANDLES[D._governor_witness_key(d,t)];during=len(os.listdir('/proc/self/fd'));state=snap(d);D.cancel_governor_reservation(SOURCE/'utilities/model-worker-governor.py',d,t);after=len(os.listdir('/proc/self/fd'))
  record('wrapper-cancel-real-FD',{'fd_delta_held':1,'fd_delta_after':0,'handles':0,'reservations':0},{'fd_delta_held':during-before,'fd_delta_after':after-before,'handles':len(D._GOVERNOR_WITNESS_HANDLES),'reservations':len(snap(d)['reservations'])},{'before_fd':before,'during_fd':during,'after_fd':after,'held_state':state,'final_state':snap(d),'witness_path_exists':h.path.exists()})
  d=root/'fork';h=G.create_witness(d,'reservation');rd,wr=os.pipe();pid=os.fork()
  if pid==0:
   os.close(rd)
   try:os.fstat(h.fd);closed=False
   except OSError:closed=True
   os.write(wr,json.dumps({'fd_closed':closed,'path_exists':h.path.exists()}).encode());os._exit(0)
  os.close(wr);child=json.loads(os.read(rd,4096));os.close(rd);_,status=os.waitpid(pid,0);held=G.observe_witness(d,h.binding());G.close_witness(h)
  record('fork-FD-lifetime',{'child_fd_closed':True,'parent_live':'live','child_exit':0},{'child_fd_closed':child['fd_closed'],'parent_live':held.state,'child_exit':os.waitstatus_to_exitcode(status)},{'child':child,'parent_observation':held.__dict__})
  # Real wrapper -> governor runner -> descendant, all in runner-owned group.
  for killed in (False,True):
   name='kill-retention' if killed else 'normal-drain-release';d=root/name;before=len(os.listdir('/proc/self/fd'));token,_=D.reserve_governor_token(SOURCE/'utilities/model-worker-governor.py',d,'dispatch')
   childcode="import os,sys,json; print(json.dumps({'child_pid':os.getpid()}),flush=True); sys.stdin.readline()"
   env=dict(os.environ);env['AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN']=token
   p=subprocess.Popen([sys.executable,str(SOURCE/'utilities/model-worker-governor.py'),'--root',str(d),'run','--class','dispatch','--',sys.executable,'-c',childcode],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
   ready=line(p);payload=D.wait_governor_reservation_claim(SOURCE/'utilities/model-worker-governor.py',d,token,p,timeout=3);claimstate=snap(d);fd_after_transfer=len(os.listdir('/proc/self/fd'))-3 # own three PIPE handles
   runnerid=ident(p.pid);childid=ident(ready['child_pid'])
   record(name+'-transfer-witness',{'claimant':'live','owner_path_exists':False,'lease_binding_matches':True},{'claimant':G.observe_witness(d,payload['claimant_witness']).state,'owner_path_exists':(d/payload['owner_witness']['relative_path']).exists(),'lease_binding_matches':claimstate['leases'][token]['claimant_witness']==payload['claimant_witness']},{'receipt':payload,'lease':claimstate['leases'][token]})
   if killed:
    p.kill();p.wait(timeout=10);alive=ident(ready['child_pid']);check=gov.reservation_check(d,token,worker_class='dispatch');retained=snap(d)
    record(name,{'runner_exit':-9,'child_alive':True,'occupied':True,'group_owned':True,'fd_delta_after_transfer':0,'typed_unknown':True},{'runner_exit':p.returncode,'child_alive':'errno' not in alive and alive.get('state')!='Z','occupied':token in retained['leases'],'group_owned':claimstate['leases'][token].get('group_owned') is True,'fd_delta_after_transfer':fd_after_transfer-before,'typed_unknown':check.get('identity_observation',{}).get('state')=='unknown' and check.get('identity_observation',{}).get('occupied') is True},{'runner':runnerid,'child':childid,'after_kill_child':alive,'check':check,'claimed_state':claimstate,'retained_state':retained,'typed_blocker':check.get('identity_observation')})
    p.stdin.write('finish\n');p.stdin.flush();os.waitpid(ready['child_pid'],0);gov.release(d,token);p.communicate(timeout=10)
   else:
    retained=snap(d);p.stdin.write('finish\n');p.stdin.flush();p.communicate(timeout=10)
    record(name,{'exit':0,'occupied_while_child_alive':True,'lease_after':False,'group_owned':True,'fd_delta_after_transfer':0,'fd_delta_final':0},{'exit':p.returncode,'occupied_while_child_alive':token in retained['leases'],'lease_after':token in snap(d)['leases'],'group_owned':claimstate['leases'][token].get('group_owned') is True,'fd_delta_after_transfer':fd_after_transfer-before,'fd_delta_final':len(os.listdir('/proc/self/fd'))-before},{'runner':runnerid,'child':childid,'claim_receipt':payload,'claimed_state':claimstate,'final_state':snap(d)})
   if killed:record('kill-descendant-exit-still-occupied-without-original-runner',True,token in snap(d)['leases'],{'final_state':snap(d),'release':gov.release(d,token)})
  dying=Actor(True,root,'namespace-exit')
  held=dying.call(cmd='hold',case='namespace-exit');exit_proof=dying.close();d=root/'namespace-exit'
  observation=gov._identity_diagnostic(d,snap(d)['leases'][held['token']]);returned=gov.release(d,held['token'])
  record('namespace-exit-remains-occupied',{'actor_exit':0,'state':'unknown','occupied':True,'released':False},{'actor_exit':exit_proof['exit'],'state':observation['state'],'occupied':held['token'] in snap(d)['leases'],'released':returned['release_proven']},{'held':held,'exit':exit_proof,'observation':observation,'return':returned})

 finally:
  exits={a.label:a.close() for a in reversed(actors)};(root/'actors.json').write_text(json.dumps(exits,indent=2)+'\n')
  (root/'provenance.json').write_text(json.dumps({'script_sha256':hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),'source':str(SOURCE),'production_changed':False,'models':0,'root':str(root),'actor_commands':{a.label:a.cmd for a in actors},'identity':ident()},indent=2)+'\n')
  print('RESULT_ROOT='+str(root),flush=True)
 return root, rows
class NamespaceBoundaryTest(unittest.TestCase):
 def test_actual_namespace_lifetime_and_returns(self):
  if os.environ.get("HEARTING_REQUIRE_PIDNS") != "1":
   self.skipTest("set HEARTING_REQUIRE_PIDNS=1 for required real OS verification")
  self.assertIsNotNone(shutil.which("bwrap"), "BLOCKED: bwrap unavailable")
  root, rows = run()
  self.assertFalse([r for r in rows if not r["pass"]], str(root / "raw.json"))

 def test_exec_and_exception_lose_witness_without_death_authority(self):
  root=Path(tempfile.mkdtemp(prefix="governor-fd-lifetime-")); rows=[]
  env=dict(os.environ);env["PYTHONPATH"]=str(SOURCE/"utilities")
  for kind in ("exec","exception"):
   code="import os,sys,json,governor_identity as G; h=G.create_witness(sys.argv[1],'claimant'); print(json.dumps(h.binding()),flush=True); "
   if kind=="exec":
    after="import os,sys,json\ntry: os.fstat(int(sys.argv[1])); closed=False\nexcept OSError: closed=True\nprint(json.dumps({'closed':closed}),flush=True)\nsys.stdin.readline()"
    code+="os.execv(sys.executable,[sys.executable,'-c',"+repr(after)+",str(h.fd)])"
   else:code+="raise RuntimeError('fixture-intentional-exception')"
   p=subprocess.Popen([sys.executable,"-c",code,str(root/kind)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
   binding=line(p)
   if kind=="exec":
    fd=line(p);self.assertTrue(fd["closed"]);observation=G.observe_witness(root/kind,binding);out,err=p.communicate("done\n",timeout=10)
   else:
    out,err=p.communicate(timeout=10);observation=G.observe_witness(root/kind,binding)
   self.assertEqual(p.returncode,0 if kind=="exec" else 1)
   self.assertEqual(observation.state,"unknown")
   rows.append({"case":kind,"exit":p.returncode,"observation":observation.__dict__,"stderr":err})
  (root/"raw.json").write_text(json.dumps(rows,indent=2)+"\n");print("LIFETIME_ROOT="+str(root),flush=True)

if __name__ == "__main__":
 if len(sys.argv)>1 and sys.argv[1]=='actor': actor()
 else: unittest.main()
