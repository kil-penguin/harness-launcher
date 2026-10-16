"""CLI contract for one-key Codex UI requests; loopback only."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import subprocess
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))
import harness_herdr_cli as cli
import harness_herdr_store as guarded_store
import harness_herdr_guarded as guarded
import harness_herdr_agent_ui_receipt as receipt

LOCAL = "11111111-1111-4111-8111-111111111111"
REMOTE = "22222222-2222-4222-8222-222222222222"
WORKFLOW = "33333333-3333-4333-8333-333333333abc"
METHODS = ["web.agent_ui_request." + name + ".v1" for name in ("prepare", "dispatch", "get", "cancel")]
CAP = {"version": 1, "methods": METHODS, "agents": ["codex"], "requests": ["interrupt", "exit"]}
TARGET = {"server_epoch":"epoch","workspace_id":"w","tab_id":"t","pane_id":"p","terminal_id":"term","identity_generation":0,"agent_binding":{"detected_agent":"codex","hook":{"source":"s","agent_label":"codex","session_ref":None},"session":{"source":"s","agent":"codex","session_ref":{"kind":"id","value":"id"}}}}

def workflow(**changes):
    return {"workflow_id": WORKFLOW, "server_epoch":"epoch", "state":"awaiting_operation", "revision":0, "current_operation":{"operation_id":WORKFLOW,"server_epoch":"epoch","state":"completed","effect":{"text_bytes_accepted":0,"enter_accepted":False}}, **changes}

class AgentUiRequestTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir="/private/tmp"); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve(); self.home.chmod(0o700)
        self.files=self.home/"files"; self.files.mkdir(mode=0o700)
        self.target=self.files/"target"; self.target.write_text(json.dumps({"device_id":LOCAL,"target":TARGET})); self.target.chmod(0o600)
        self.calls=[]; self.stall=False; self.drop_post=None; outer=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.calls.append(("GET",self.path,dict(self.headers),b""))
                if self.path.endswith("/identity"):
                    device=REMOTE if "/machines/" in self.path else LOCAL
                    value={"protocol":1,"device_id":device,"native":{"version":"x"},"capabilities":{"read_metadata_v1":True,"guarded_mutation_v1":True,"web_guarded_agent_ui_request_v1":CAP}}
                else: value=workflow()
                self.reply(value)
            def do_POST(self):
                raw=self.rfile.read(int(self.headers.get("Content-Length","0"))); outer.calls.append(("POST",self.path,dict(self.headers),raw))
                if outer.stall or outer.drop_post==len([x for x in outer.calls if x[0]=="POST"]): return
                self.reply(workflow())
            def reply(self,value):
                raw=json.dumps(value).encode(); self.send_response(200); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def log_message(self,*args): pass
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler); threading.Thread(target=self.server.serve_forever,daemon=True).start(); self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close()))
        self.client=cli.local.Client("a"*64,"plugin",self.server.server_address[1])
    def run_cli(self,args):
        out=io.StringIO()
        with patch.object(cli.local,"discover",return_value=self.client),patch.object(Path,"home",return_value=self.home),patch("sys.stdout",out): code=cli.main(args)
        return code,[json.loads(x) for x in out.getvalue().splitlines()]
    def test_interrupt_and_exit_use_exact_routes_and_one_key_request_enum(self):
        for command,request in (("agent-interrupt","interrupt"),("agent-exit","exit")):
            code,rows=self.run_cli([command,"--device",LOCAL,"--target-file",str(self.target),"--new-operation"])
            self.assertEqual(code,0,rows); intent=rows[0]["intent_uuid"]
            post=[x for x in self.calls if x[0]=="POST"]
            self.assertEqual([x[1] for x in post],["/api/cli/v1/agent/ui-request/prepare",f"/api/cli/v1/agent/ui-request/workflows/{WORKFLOW}/dispatch"])
            self.assertEqual(json.loads(post[0][3]),{"method":METHODS[0],"request":request,"target":TARGET})
            self.assertEqual(json.loads(post[1][3]),{"method":METHODS[1],"expected_revision":0})
            self.assertEqual(self.run_cli(["status","--intent",intent])[0],0)
            self.assertEqual(self.run_cli(["cancel","--intent",intent])[0],0)
            self.calls.clear()
    def test_target_file_must_be_full_codex_target_and_reads_before_http(self):
        self.target.write_text(json.dumps({"device_id":LOCAL,"target":{"pane_id":"p"}}))
        self.assertEqual(self.run_cli(["agent-interrupt","--device",LOCAL,"--target-file",str(self.target),"--new-operation"])[0],1)
        self.assertEqual(self.calls,[])
    def test_remote_uses_machine_prefix_and_header(self):
        self.target.write_text(json.dumps({"device_id":REMOTE,"target":TARGET}))
        self.assertEqual(self.run_cli(["agent-exit","--device",REMOTE,"--target-file",str(self.target),"--new-operation"])[0],0)
        posts=[x for x in self.calls if x[0]=="POST"]
        self.assertTrue(all(x[1].startswith(f"/api/machines/{REMOTE}/cli/v1/") and x[2].get("X-Herdr-Machine")=="1" for x in posts))
    def test_shared_absolute_deadline_stops_after_prepare_without_dispatch_replay(self):
        self.stall=True; started=time.monotonic()
        with patch.object(cli.local,"REQUEST_DEADLINE_SECONDS",.1):
            code,rows=self.run_cli(["agent-interrupt","--device",LOCAL,"--target-file",str(self.target),"--new-operation"])
        self.assertEqual(code,1); self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(len([x for x in self.calls if x[0]=="POST"]),1)
        intent=rows[0]["intent_uuid"]; self.stall=False; self.calls.clear()
        self.assertEqual(self.run_cli(["status","--intent",intent])[0],1)
        self.assertEqual(self.calls,[])
    def test_lost_dispatch_and_cancel_are_never_replayed(self):
        self.drop_post=2
        code,rows=self.run_cli(["agent-interrupt","--device",LOCAL,"--target-file",str(self.target),"--new-operation"])
        self.assertEqual(code,1); intent=rows[0]["intent_uuid"]
        self.assertEqual(len([x for x in self.calls if x[0]=="POST"]),2)
        self.drop_post=None; self.calls.clear()
        self.assertEqual(self.run_cli(["status","--intent",intent])[0],0)
        self.calls.clear(); self.assertEqual(self.run_cli(["agent-interrupt","--device",LOCAL,"--target-file",str(self.target),"--new-operation"])[0],0)
        intent=self.run_cli(["agent-exit","--device",LOCAL,"--target-file",str(self.target),"--new-operation"])[1][0]["intent_uuid"]
        self.calls.clear(); self.drop_post=1
        self.assertEqual(self.run_cli(["cancel","--intent",intent])[0],1); self.assertEqual(len([x for x in self.calls if x[0]=="POST"]),1)
        self.drop_post=None; self.calls.clear(); self.assertEqual(self.run_cli(["status","--intent",intent])[0],0); self.assertFalse(any(x[0]=="POST" for x in self.calls))
        self.calls.clear(); self.assertEqual(self.run_cli(["cancel","--intent",intent])[0],1); self.assertFalse(any(x[0]=="POST" for x in self.calls))
    def test_global_namespace_conflict_is_zero_http(self):
        code,rows=self.run_cli(["agent-interrupt","--device",LOCAL,"--target-file",str(self.target),"--new-operation"]); self.assertEqual(code,0)
        intent=rows[0]["intent_uuid"]
        guarded_db=guarded_store.Store(home=self.home); guarded_db.create_intent(intent,LOCAL,guarded.METHOD)
        agent_db=cli.agent_ui_receipt.Store(home=self.home)
        self.calls.clear()
        with patch.object(cli.store,"Store",return_value=guarded_db), patch.object(cli.agent_ui_receipt,"Store",return_value=agent_db):
            self.assertEqual(self.run_cli(["status","--intent",intent])[0],1)
        self.assertFalse(self.calls)
    def test_receipt_capacity_refuses_65th_without_evicting_live_no_replay_record(self):
        database=receipt.Store(home=self.home)
        for index in range(64):
            database.create(f"33333333-3333-4333-8333-{index:012d}",LOCAL,"interrupt","epoch")
        with self.assertRaises(receipt.ReceiptError): database.create("33333333-3333-4333-8333-000000000064",LOCAL,"interrupt","epoch")
        self.assertEqual(len(list(database.root.glob("*.json"))),64)

    def test_concurrent_64th_admission_has_one_winner_and_never_65_records(self):
        database=receipt.Store(home=self.home)
        for index in range(63): database.create(f"33333333-3333-4333-8333-{index:012d}",LOCAL,"interrupt","epoch")
        gate=threading.Barrier(2); errors=[]
        def create(index):
            gate.wait()
            try: database.create(f"33333333-3333-4333-8333-{index:012d}",LOCAL,"interrupt","epoch")
            except Exception as error: errors.append(str(error))
        threads=[threading.Thread(target=create,args=(index,)) for index in (63,64)]
        for item in threads:item.start()
        for item in threads:item.join()
        self.assertEqual(len(list(database.root.glob("*.json"))),64); self.assertEqual(errors,["receipt_capacity"])
    def test_same_content_replaced_inode_fails_transition_cas(self):
        database=receipt.Store(home=self.home); intent="33333333-3333-4333-8333-000000000000"; record=database.create(intent,LOCAL,"interrupt","epoch")
        path=database.root/(intent+".json"); raw=path.read_bytes(); replacement=database.root/"replacement"
        replacement.write_bytes(raw); replacement.chmod(0o600); os.replace(replacement,path)
        with self.assertRaises(receipt.ReceiptError): database.transition(record,"prepare_started")

    def test_actual_subprocess_six_crash_boundaries_never_replay(self):
        source = r'''import json,os,stat,sys
from pathlib import Path
sys.path.insert(0,sys.argv.pop(1)); import harness_herdr_cli as c
port=int(sys.argv.pop(1)); stage=sys.argv.pop(1)
root=Path.home()/'.local/state/harness-herdr-web/agent-ui-request-receipts/v1'; original=os.fsync
def sync(fd):
 original(fd)
 if not stat.S_ISDIR(os.fstat(fd).st_mode): return
 rows=list(root.glob('*.json'))
 if not rows: return
 value=json.loads(rows[0].read_text())
 if stage=='intent' and value['phase']=='intent_durable': os._exit(42)
 if stage=='prepare_marker' and value['phase']=='prepare_started': os._exit(42)
 if stage=='dispatch_marker' and value['phase']=='dispatch_started': os._exit(42)
os.fsync=sync
if stage=='start_flush':
 class Output:
  def write(self,v): return sys.__stdout__.write(v)
  def flush(self): sys.__stdout__.flush(); os._exit(42)
 sys.stdout=Output()
orig=c.agent_ui_request.checked
def checked(value,record):
 out=orig(value,record)
 if stage=='prepare_reply' and record['workflow_id'] is None: os._exit(42)
 if stage=='dispatch_reply' and record['phase']=='dispatch_started': os._exit(42)
 return out
c.agent_ui_request.checked=checked
c.local.discover=lambda:c.local.Client('a'*64,'plugin',port)
sys.exit(c.main(['agent-interrupt','--device','11111111-1111-4111-8111-111111111111','--target-file',str(Path.home()/'files'/'target'),'--new-operation']))'''
        expected={'intent':0,'start_flush':0,'prepare_marker':0,'prepare_reply':1,'dispatch_marker':1,'dispatch_reply':2}
        for stage,count in expected.items():
            stage_home=self.home/stage; stage_home.mkdir(mode=0o700); stage_files=stage_home/'files'; stage_files.mkdir(mode=0o700)
            target=stage_files/'target'; target.write_text(self.target.read_text()); target.chmod(0o600)
            self.calls.clear(); environment={**os.environ,'HOME':str(stage_home),'PYTHONDONTWRITEBYTECODE':'1'}
            done=subprocess.run([sys.executable,'-c',source,str(Path(__file__).parents[1]/'bin'),str(self.server.server_address[1]),stage],capture_output=True,text=True,env=environment,timeout=5)
            self.assertEqual(done.returncode,42,(stage,done.stdout,done.stderr)); self.assertEqual(len([x for x in self.calls if x[0]=='POST']),count,stage)
            if count:
                intent=json.loads(done.stdout.splitlines()[0])['intent_uuid']
                recover="import sys;sys.path.insert(0,sys.argv.pop(1));import harness_herdr_cli as c;port=int(sys.argv.pop(1));c.local.discover=lambda:c.local.Client('a'*64,'plugin',port);sys.exit(c.main(sys.argv[1:]))"
                before=len([x for x in self.calls if x[0]=='POST'])
                status=subprocess.run([sys.executable,'-c',recover,str(Path(__file__).parents[1]/'bin'),str(self.server.server_address[1]),'status','--intent',intent],capture_output=True,text=True,env=environment,timeout=5)
                self.assertIn(status.returncode,(0,1),stage); self.assertEqual(len([x for x in self.calls if x[0]=='POST']),before,stage)
                first=subprocess.run([sys.executable,'-c',recover,str(Path(__file__).parents[1]/'bin'),str(self.server.server_address[1]),'cancel','--intent',intent],capture_output=True,text=True,env=environment,timeout=5)
                after=len([x for x in self.calls if x[0]=='POST'])
                second=subprocess.run([sys.executable,'-c',recover,str(Path(__file__).parents[1]/'bin'),str(self.server.server_address[1]),'cancel','--intent',intent],capture_output=True,text=True,env=environment,timeout=5)
                self.assertIn(first.returncode,(0,1),stage); self.assertIn(second.returncode,(0,1),stage); self.assertEqual(len([x for x in self.calls if x[0]=='POST']),after,stage)

    def test_parent_fsync_and_brokenpipe_stop_before_prepare_post(self):
        args=['agent-interrupt','--device',LOCAL,'--target-file',str(self.target),'--new-operation']
        real=os.fsync
        def failed(fd): raise OSError('private-fsync')
        with patch.object(os,'fsync',side_effect=failed):
            self.assertEqual(self.run_cli(args)[0],1)
        self.assertFalse(any(x[0]=='POST' for x in self.calls))
        class Broken(io.StringIO):
            def flush(self): raise BrokenPipeError()
        output=Broken()
        with patch.object(cli.local,'discover',return_value=self.client),patch.object(Path,'home',return_value=self.home),patch('sys.stdout',output):
            self.assertEqual(cli.main(args),1)
        self.assertFalse(any(x[0]=='POST' for x in self.calls))

    def test_parent_fsync_after_rename_prepare_marker_stops_before_post(self):
        args=['agent-interrupt','--device',LOCAL,'--target-file',str(self.target),'--new-operation']; original=os.fsync
        def parent_failure(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                root=self.home/'.local/state/harness-herdr-web/agent-ui-request-receipts/v1'
                rows=list(root.glob('*.json')) if root.exists() else []
                if rows and json.loads(rows[0].read_text())['phase']=='prepare_started': raise OSError('parent-fsync')
            original(fd)
        with patch.object(os,'fsync',side_effect=parent_failure): self.assertEqual(self.run_cli(args)[0],1)
        self.assertFalse(any(x[0]=='POST' for x in self.calls))

    def test_closed_native_result_states_reasons_and_counter_bounds(self):
        base=workflow(); valid=[]
        for state in ('prepared','queued','writing_text','waiting_enter','writing_enter','completed','cancelled','partial_unknown','rejected'):
            valid.append({**base,'current_operation':{**base['current_operation'],'state':state}})
        for value in valid: self.assertEqual(receipt.result(value)['current_operation']['state'],value['current_operation']['state'])
        for reason in ('guard_mismatch','deadline_exceeded','pty_closed','io_error','cancelled','partial_unknown'):
            value={**base,'current_operation':{**base['current_operation'],'reason':reason}}; self.assertEqual(receipt.result(value)['current_operation']['reason'],reason)
        bad=[{**base,'extra':1},{**base,'revision':-1},{**base,'revision':1<<64},{**base,'current_operation':{**base['current_operation'],'state':'forged'}},{**base,'current_operation':{**base['current_operation'],'effect':{'text_bytes_accepted':True,'enter_accepted':False}}},{**base,'current_operation':{**base['current_operation'],'reason':'forged'}}]
        for value in bad:
            with self.assertRaises(receipt.ReceiptError): receipt.result(value)

if __name__ == "__main__": unittest.main()
