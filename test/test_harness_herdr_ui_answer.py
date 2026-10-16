"""UI consumer contracts: main, real loopback and private durable state only."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import signal
import subprocess
import time
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'bin'))
import harness_herdr_cli as cli

LOCAL = '11111111-1111-4111-8111-111111111111'
REMOTE = '22222222-2222-4222-8222-222222222222'
WORKFLOW = '33333333-3333-4333-8333-333333333abc'
METHODS = ['web.ui_answer.' + name + '.v1' for name in ('prepare', 'step', 'get', 'cancel')]
CAP = {'version': 1, 'methods': METHODS, 'agents': ['claude']}
TARGET = {'server_epoch': 'epoch', 'workspace_id': 'w', 'tab_id': 't', 'pane_id': 'p', 'terminal_id': 'term', 'identity_generation': (1 << 63) - 1, 'agent_binding': {'detected_agent': 'claude', 'hook': None, 'session': None}}

def workflow(**changes):
    return {'workflow_id': WORKFLOW, 'server_epoch': 'epoch', 'state': 'ready', 'next_step': 0, 'step_count': 2, 'revision': 0, **changes}

class UiAnswerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp'); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve(); self.home.chmod(0o700)
        self.files = self.home / 'files'; self.files.mkdir(mode=0o700)
        for name, value in [('target', {'device_id': LOCAL, 'target': TARGET}), ('prompt', {'pane_id': 'p', 'prompt_id': 'abcdef012345'}), ('answer', {'option_index': 1})]:
            self.write_file(name, value)
        self.calls = []; self.reply = workflow(); self.cap = copy.deepcopy(CAP); self.snapshot_epoch = 'epoch'; self.on_post = None; self.stall = None; self.stalled = threading.Event(); self.socket_closed = threading.Event()
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.calls.append(('GET', self.path, dict(self.headers), self.rfile.read(int(self.headers.get('Content-Length', '0')))))
                if self.path.endswith('/identity'):
                    device = REMOTE if '/machines/' in self.path else LOCAL
                    value = {'protocol': 1, 'device_id': device, 'native': {'version': '378'}, 'capabilities': {'read_metadata_v1': True, 'guarded_mutation_v1': True, 'web_guarded_ui_answer_v1': outer.cap}}
                elif self.path.endswith('/snapshot'):
                    value = {'protocol': 1, 'device_id': REMOTE if '/machines/' in self.path else LOCAL, 'server_epoch': outer.snapshot_epoch, 'workspaces': [], 'tabs': [], 'panes': []}
                else: value = outer.reply
                self.send(value)
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                outer.calls.append(('POST', self.path, dict(self.headers), raw))
                if outer.on_post: outer.on_post()
                if outer.stall:
                    if outer.stall == 'body':
                        self.send_response(200); self.send_header('Content-Length','200'); self.end_headers(); self.wfile.write(b'{'); self.wfile.flush()
                    outer.stalled.set(); self.connection.settimeout(4)
                    try:
                        if self.connection.recv(1) == b'': outer.socket_closed.set()
                    except ConnectionResetError: outer.socket_closed.set()
                    except OSError: pass
                    return
                self.send(outer.reply)
            def send(self, value):
                if value is None: self.close_connection = True; return
                status = 200
                if isinstance(value, tuple): status, value = value
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=lambda:self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.client = cli.local.Client('a' * 64, 'plugin', self.server.server_address[1])

    def write_file(self, name, value):
        path = self.files / name; path.write_text(json.dumps(value)); path.chmod(0o600); return str(path)

    def run_cli(self, args):
        out = io.StringIO()
        with patch.object(cli.local, 'discover', return_value=self.client), patch.object(Path, 'home', return_value=self.home), patch('sys.stdout', out), patch('sys.stderr', io.StringIO()):
            try: code = cli.main(args)
            except SystemExit as error: code = error.code
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def prepare(self, device=LOCAL):
        self.write_file('target', {'device_id': device, 'target': TARGET})
        code, rows = self.run_cli(['ui-answer-prepare', '--device', device, '--target-file', str(self.files/'target'), '--prompt-file', str(self.files/'prompt'), '--answer-file', str(self.files/'answer'), '--new-workflow'])
        self.assertEqual(code, 0, rows)
        return rows[0]['intent_uuid']

    def step(self, intent, step='0', revision='0'):
        return self.run_cli(['ui-answer-step', '--intent', intent, '--expected-step', step, '--expected-revision', revision])

    def test_main_local_remote_four_exact_routes_and_no_automatic_step(self):
        for device in (LOCAL, REMOTE):
            self.reply = workflow(); intent = self.prepare(device)
            posts = [r for r in self.calls if r[0] == 'POST']; self.assertEqual(len(posts), 1)
            body = json.loads(posts[0][3]); self.assertEqual(body, {'method': METHODS[0], 'server_epoch': 'epoch', 'target': TARGET, 'prompt': {'pane_id': 'p', 'prompt_id': 'abcdef012345'}, 'answer': {'option_index': 1}})
            self.reply = workflow(next_step=1, revision=(1<<64)-1, current_operation={'operation_id': WORKFLOW, 'server_epoch': 'epoch', 'state': 'completed', 'effect': {'text_bytes_accepted': (1<<64)-1, 'enter_accepted': True}})
            code, rows = self.step(intent); self.assertEqual(code, 0, rows); self.assertEqual(rows[-1]['revision'], (1<<64)-1)
            self.assertEqual(self.run_cli(['ui-answer-status', '--intent', intent])[0], 0)
            self.assertEqual(self.run_cli(['ui-answer-cancel', '--intent', intent])[0], 0)
            prefix = '/api/cli/v1' if device == LOCAL else f'/api/machines/{device}/cli/v1'
            ops = [r for r in self.calls if '/ui-answer/' in r[1]]
            self.assertEqual([(r[0], r[1]) for r in ops], [('POST', prefix+'/agent/ui-answer/prepare'), ('POST', prefix+f'/agent/ui-answer/workflows/{WORKFLOW}/step'), ('GET', prefix+f'/agent/ui-answer/workflows/{WORKFLOW}'), ('POST', prefix+f'/agent/ui-answer/workflows/{WORKFLOW}/cancel')])
            self.assertEqual(ops[2][3], b'')
            if device == REMOTE: self.assertTrue(all(r[2].get('X-Herdr-Machine') == '1' for r in ops))
            self.calls.clear()

    def test_invalid_canonical_counters_are_http_zero_even_5000_digits(self):
        for number in ('00', '-1', '+1', '1.0', '١', '9'*5000, str(1<<64)):
            code, _ = self.step(LOCAL, revision=number); self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])

    def test_missing_or_malformed_capability_never_posts(self):
        for cap in (None, {}, {**CAP, 'extra': 'SECRET'}, {**CAP, 'version': True}, {**CAP, 'methods': METHODS[:-1]}, {**CAP, 'agents': ['codex']}):
            self.cap = cap
            code, rows = self.run_cli(['ui-answer-prepare','--device',LOCAL,'--target-file',str(self.files/'target'),'--prompt-file',str(self.files/'prompt'),'--answer-file',str(self.files/'answer'),'--new-workflow'])
            self.assertEqual(code, 1, rows); self.assertEqual(rows[-1]['error']['code'], 'guard_not_supported')
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_lost_prepare_has_no_id_status_cancel_network_zero(self):
        self.reply = None
        code, rows = self.run_cli(['ui-answer-prepare','--device',LOCAL,'--target-file',str(self.files/'target'),'--prompt-file',str(self.files/'prompt'),'--answer-file',str(self.files/'answer'),'--new-workflow'])
        self.assertEqual(code, 1); intent = rows[0]['intent_uuid']; self.calls.clear()
        for command in ('status','cancel'):
            self.assertEqual(self.run_cli(['ui-answer-'+command,'--intent',intent]), (1,[{'error':{'code':'workflow_unknown'}}]))
        self.assertEqual(self.calls, [])

    def test_lost_step_remains_get_cancel_only_after_ready_get(self):
        intent = self.prepare(); self.reply = None
        self.assertEqual(self.step(intent)[0], 1)
        self.reply = workflow(next_step=1, revision=2)
        self.assertEqual(self.run_cli(['ui-answer-status','--intent',intent])[0], 0)
        self.calls.clear(); self.assertEqual(self.step(intent, '1','2')[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))
        self.assertEqual(self.run_cli(['ui-answer-cancel','--intent',intent])[0],0)

    def test_bad_private_answer_and_epoch_prevent_post(self):
        for value in ({'custom_text':'SECRET'},{'option_index':True},{'option_index':0,'keys':['Enter']}):
            self.write_file('answer',value)
            code,_ = self.run_cli(['ui-answer-prepare','--device',LOCAL,'--target-file',str(self.files/'target'),'--prompt-file',str(self.files/'prompt'),'--answer-file',str(self.files/'answer'),'--new-workflow']); self.assertEqual(code,1)
        self.assertEqual(self.calls, [])

    def test_malformed_success_is_redacted_and_permanently_bans_step(self):
        for bad in (b'{"SECRET":', b'{"a":1,"a":2}', b'X'*65537, workflow(extra='SECRET'), workflow(workflow_id=WORKFLOW.upper()), workflow(server_epoch='SECRET'), workflow(state='provider_accepted'), workflow(revision=True)):
            self.reply=workflow(); intent=self.prepare(); self.reply=bad
            code, rows=self.step(intent); self.assertEqual(code,1); self.assertNotIn('SECRET',json.dumps(rows))
            self.reply=workflow(); self.run_cli(['ui-answer-status','--intent',intent]); self.calls.clear()
            self.assertEqual(self.step(intent)[0],1); self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_lost_cancel_never_replays_after_get(self):
        intent = self.prepare(); self.reply = None
        self.assertEqual(self.run_cli(['ui-answer-cancel','--intent',intent])[0], 1)
        self.reply = workflow(state='cancelled')
        self.run_cli(['ui-answer-status','--intent',intent]); self.calls.clear()
        self.assertEqual(self.run_cli(['ui-answer-cancel','--intent',intent])[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_nonready_step_result_never_authorizes_another_step_after_get(self):
        intent=self.prepare(); self.reply=workflow(state='awaiting_operation', revision=1)
        self.assertEqual(self.step(intent)[0],0)
        self.reply=workflow(next_step=1, revision=2)
        self.assertEqual(self.run_cli(['ui-answer-status','--intent',intent])[0],0)
        self.calls.clear(); self.assertEqual(self.step(intent,'1','2')[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_unchanged_ready_step_response_is_not_replay_authority(self):
        intent=self.prepare()
        self.assertEqual(self.step(intent)[0],1)
        self.calls.clear(); self.assertEqual(self.step(intent)[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_private_files_are_read_once_and_target_epoch_is_selected_snapshot(self):
        actual=cli.files._read; reads=[]
        def observe(path, size): reads.append(path); return actual(path,size)
        with patch.object(cli.files,'_read',side_effect=observe): self.prepare(REMOTE)
        self.assertEqual(sorted(reads),sorted(str(self.files/n) for n in ('target','prompt','answer')))
        self.calls.clear(); self.snapshot_epoch=None
        code,_=self.run_cli(['ui-answer-prepare','--device',REMOTE,'--target-file',str(self.files/'target'),'--prompt-file',str(self.files/'prompt'),'--answer-file',str(self.files/'answer'),'--new-workflow'])
        self.assertEqual(code,1); self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_start_is_fsynced_and_flushed_before_post(self):
        events=[]; original=os.fsync
        class Output(io.StringIO):
            def flush(self): events.append('flush'); super().flush()
        output=Output()
        def sync(fd): events.append('fsync'); original(fd)
        self.on_post=lambda: events.append('post')
        args=cli.parser().parse_args(['ui-answer-prepare','--device',LOCAL,'--target-file',str(self.files/'target'),'--prompt-file',str(self.files/'prompt'),'--answer-file',str(self.files/'answer'),'--new-workflow'])
        with patch.object(Path,'home',return_value=self.home), patch.object(os,'fsync',side_effect=sync): cli.execute_args(args,self.client,stdout=output)
        first=events.index('post'); self.assertIn('fsync',events[:first]); self.assertEqual(events[first-1],'flush')
        records=list((self.home/'.local/state/harness-herdr-web/ui-answer-receipts/v1').glob('*.json'))
        raw=records[0].read_text(); self.assertNotIn('prompt',raw); self.assertNotIn('option_index',raw); self.assertNotIn('session',raw)
        self.assertEqual(records[0].stat().st_mode & 0o777,0o600)

    def test_flush_failure_and_reservation_failure_make_post_zero(self):
        args=cli.parser().parse_args(['ui-answer-prepare','--device',LOCAL,'--target-file',str(self.files/'target'),'--prompt-file',str(self.files/'prompt'),'--answer-file',str(self.files/'answer'),'--new-workflow'])
        class Output(io.StringIO):
            def flush(self): raise BrokenPipeError('SECRET')
        with patch.object(Path,'home',return_value=self.home):
            with self.assertRaises(BrokenPipeError): cli.execute_args(args,self.client,stdout=Output())
        self.assertFalse(any(r[0]=='POST' for r in self.calls))
        with patch.object(os,'fsync',side_effect=OSError('SECRET')):
            self.assertEqual(self.run_cli(vars_to_args(args))[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_response_tuple_replacement_during_step_is_rejected(self):
        from harness_herdr_ui_receipt import UIReceiptStore
        intent=self.prepare(); database=UIReceiptStore(self.home)
        def replace():
            record=database.read(intent)
            database.transition(record,phase='uncertain',attempt=record['attempt'])
        self.reply=workflow(next_step=1,revision=2); self.on_post=replace
        code,rows=self.step(intent); self.assertEqual(code,1); self.assertEqual(rows[-1]['error']['code'],'receipt_changed')
        self.on_post=None; self.calls.clear(); self.assertEqual(self.step(intent,'1','2')[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_concurrent_same_step_has_one_post_and_no_network_lock(self):
        from harness_herdr_ui_receipt import UIReceiptStore
        intent=self.prepare(); self.calls.clear(); entered=threading.Event(); release=threading.Event()
        def block(): entered.set(); release.wait(3)
        self.on_post=block; self.reply=workflow(next_step=1,revision=2)
        args=cli.parser().parse_args(['ui-answer-step','--intent',intent,'--expected-step','0','--expected-revision','0'])
        outcomes=[]
        def run():
            try: cli.execute_args(args,self.client,stdout=io.StringIO()); outcomes.append('ok')
            except Exception as error: outcomes.append(str(error))
        with patch.object(Path,'home',return_value=self.home):
            first=threading.Thread(target=run); first.start(); self.assertTrue(entered.wait(2))
            # This read succeeds while the first POST waits; no lock spans await.
            self.assertEqual(UIReceiptStore(self.home).read(intent)['phase'],'step_started')
            second=threading.Thread(target=run); second.start(); second.join(2)
            self.assertFalse(second.is_alive()); release.set(); first.join(2)
        self.assertEqual(len([r for r in self.calls if r[0]=='POST']),1)
        self.assertCountEqual(outcomes,['ok','reconcile_only'])

    def test_fixed_web_error_allowlist_and_unknown_error_redaction(self):
        for status,code in ((403,'revoked'),(404,'workflow_unknown'),(409,'workflow_conflict'),(409,'prompt_changed'),(429,'operation_admission_full'),(502,'guarded_native_unavailable'),(409,'untrusted_SECRET')):
            self.reply=workflow(); intent=self.prepare()
            self.reply=(status,{'error':{'code':code,'message':'SECRET'}})
            result,rows=self.step(intent); self.assertEqual(result,1)
            self.assertEqual(rows[-1]['error']['code'],code if code!='untrusted_SECRET' else 'guarded_native_unavailable'); self.assertNotIn('SECRET',json.dumps(rows))

    def test_transport_rejects_query_body_alias_and_wrong_method_http_zero(self):
        path=f'/api/cli/v1/agent/ui-answer/workflows/{WORKFLOW}'
        for method,route,body in [('GET',path+'?x=1',None),('GET',path,{}),('POST',path,{}),('GET',path+'/cancel',None),('POST','/api/machines/local/cli/v1/agent/ui-answer/prepare',{})]:
            with self.assertRaises(cli.local.TransportError): self.client.ui_json(method,route,body)
        self.assertEqual(self.calls,[])

    def test_abbreviated_ui_options_do_not_admit_a_workflow(self):
        args=['ui-answer-prepare','--dev',LOCAL,'--target-file',str(self.files/'target'),'--prompt-file',str(self.files/'prompt'),'--answer-file',str(self.files/'answer'),'--new-workflow']
        code,_=self.run_cli(args); self.assertEqual(code,2)
        self.assertEqual(self.calls,[])

    def test_durable_crash_boundaries_never_replay_after_ready_get(self):
        for stage in ('reserved','marker','flush','reply','published','unlink'):
            self.reply=workflow(); intent=self.prepare(); self.calls.clear()
            self.reply=workflow(next_step=1,revision=2)
            source = r"""
import io,json,os,stat,sys
from pathlib import Path
sys.path.insert(0,sys.argv.pop(1))
import harness_herdr_cli as c
port=int(sys.argv.pop(1)); stage=sys.argv.pop(1); intent=sys.argv.pop(1)
root=Path.home()/'.local/state/harness-herdr-web/ui-answer-receipts/v1'
path=root/(intent+'.json'); marker=root/(intent+'.step-ban')
original=os.fsync

def sync(fd):
    original(fd)
    value=json.loads(path.read_text())
    directory=stat.S_ISDIR(os.fstat(fd).st_mode)
    if stage=='reserved' and directory and value['phase']=='step_started' and not marker.exists(): os._exit(42)
    if stage=='marker' and directory and value['phase']=='step_started' and marker.exists(): os._exit(42)
    if stage=='published' and directory and value.get('last_result',{}).get('revision')==2: os._exit(42)
os.fsync=sync
if stage=='unlink':
    original_unlink=os.unlink
    def unlink(name,*args,**kwargs):
        if name==intent+'.step-ban': os._exit(42)
        return original_unlink(name,*args,**kwargs)
    os.unlink=unlink
if stage=='flush':
    class Output:
        def write(self,value): return sys.__stdout__.write(value)
        def flush(self): sys.__stdout__.flush(); os._exit(42)
    sys.stdout=Output()
if stage=='reply':
    original_checked=c.ui_answer.checked_workflow
    def checked(value,record):
        result=original_checked(value,record)
        if record['phase']=='step_started': os._exit(42)
        return result
    c.ui_answer.checked_workflow=checked
c.local.discover=lambda:c.local.Client('a'*64,'plugin',port)
sys.exit(c.main(['ui-answer-step','--intent',intent,'--expected-step','0','--expected-revision','0']))
"""
            environment={**os.environ,'HOME':str(self.home),'PYTHONDONTWRITEBYTECODE':'1'}
            completed=subprocess.run([sys.executable,'-c',source,str(Path(__file__).parents[1]/'bin'),str(self.server.server_address[1]),stage,intent],capture_output=True,text=True,env=environment,timeout=4)
            self.assertEqual(completed.returncode,42,(stage,completed.stdout,completed.stderr))
            self.assertEqual(len([r for r in self.calls if r[0]=='POST']),0 if stage in {'reserved','marker','flush'} else 1,stage)
            self.assertEqual(self.run_cli(['ui-answer-status','--intent',intent])[0],0,stage)
            self.calls.clear(); self.assertEqual(self.step(intent,'1','2')[0],1,stage)
            self.assertFalse(any(r[0]=='POST' for r in self.calls),stage)

    def test_device_epoch_workflow_replacement_cannot_be_adopted_after_await(self):
        from harness_herdr_ui_receipt import UIReceiptStore
        for field,value in (('device_id',REMOTE),('server_epoch','other'),('workflow_id',LOCAL)):
            self.on_post=None; self.reply=workflow(); intent=self.prepare(); database=UIReceiptStore(self.home)
            def replace():
                path=database.root/(intent+'.json'); record=json.loads(path.read_text()); record[field]=value
                if field=='server_epoch': record['last_result']['server_epoch']=value
                if field=='workflow_id': record['last_result']['workflow_id']=value
                path.write_text(json.dumps(record))
            self.on_post=replace; self.reply=workflow(next_step=1,revision=2)
            code,rows=self.step(intent)
            self.assertEqual(code,1,field); self.assertEqual(rows[-1]['error']['code'],'receipt_changed',field)
            self.assertFalse(any('workflow_id' in row for row in rows))

    def test_full_u64_expected_revision_is_exact_in_actual_post(self):
        intent=self.prepare(); self.reply=workflow(revision=(1<<64)-2)
        self.assertEqual(self.run_cli(['ui-answer-status','--intent',intent])[0],0)
        self.calls.clear(); self.reply=workflow(next_step=1,revision=(1<<64)-1)
        code,rows=self.step(intent,'0',str((1<<64)-2)); self.assertEqual(code,0,rows)
        body=[r[3] for r in self.calls if r[0]=='POST'][0]
        self.assertIn(b'"expected_revision":18446744073709551614',body)
        self.assertEqual(rows[-1]['revision'],18446744073709551615)

    def test_stale_explicit_step_pair_is_network_zero(self):
        intent=self.prepare(); self.reply=workflow(next_step=1,revision=1)
        self.assertEqual(self.step(intent)[0],0); self.calls.clear()
        self.assertEqual(self.step(intent,'0','0')[0],1)
        self.assertEqual(self.calls,[])

    def test_status_tuple_mismatch_never_grants_step_or_cancel_authority(self):
        for changed in ({'server_epoch':'different'},{'workflow_id':LOCAL}):
            self.reply=workflow(); intent=self.prepare(); self.reply=workflow(**changed)
            self.assertEqual(self.run_cli(['ui-answer-status','--intent',intent])[0],1)
            self.reply=workflow(); self.calls.clear()
            self.assertEqual(self.step(intent)[0],1)
            self.assertEqual(self.run_cli(['ui-answer-cancel','--intent',intent])[0],1)
            self.assertFalse(any(r[0]=='POST' for r in self.calls))
            # GET retains only the original recorded workflow locator.
            self.assertEqual(self.run_cli(['ui-answer-status','--intent',intent])[0],0)
            self.assertTrue(self.calls[-1][1].endswith('/'+WORKFLOW))

    def test_real_stalled_headers_and_body_absolute_deadline_close_socket(self):
        for point in ('headers','body'):
            self.reply=workflow(); self.stall=None; intent=self.prepare(); self.calls.clear()
            self.stall=point; self.socket_closed.clear(); self.stalled.clear()
            started=time.monotonic()
            with patch.object(cli.local,'REQUEST_DEADLINE_SECONDS',0.15):
                code,rows=self.step(intent)
            self.assertEqual(code,1); self.assertEqual(rows[-1]['error']['code'],'deadline_exceeded')
            self.assertLess(time.monotonic()-started,0.5)
            self.assertTrue(self.socket_closed.wait(1))
            self.stall=None; self.reply=workflow(next_step=1,revision=2)
            self.run_cli(['ui-answer-status','--intent',intent]); self.calls.clear()
            self.assertEqual(self.step(intent,'1','2')[0],1)
            self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_ctrl_c_closes_socket_and_permanently_bans_later_step(self):
        intent=self.prepare(); self.stall='body'; self.calls.clear()
        source="import sys; sys.path.insert(0,sys.argv.pop(1)); import harness_herdr_cli as c; c.local.discover=lambda:c.local.Client('a'*64,'plugin',port); port=int(sys.argv.pop(1)); sys.exit(c.main(sys.argv[1:]))"
        args=['ui-answer-step','--intent',intent,'--expected-step','0','--expected-revision','0']
        environment={**os.environ,'HOME':str(self.home),'PYTHONDONTWRITEBYTECODE':'1'}
        proc=subprocess.Popen([sys.executable,'-c',source,str(Path(__file__).parents[1]/'bin'),str(self.server.server_address[1]),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment)
        try:
            self.assertTrue(self.stalled.wait(3)); proc.send_signal(signal.SIGINT)
            stdout,stderr=proc.communicate(timeout=3)
            self.assertEqual(proc.returncode,1,(stdout,stderr)); self.assertNotIn('Traceback',stderr)
            self.assertTrue(self.socket_closed.wait(1))
        finally:
            if proc.poll() is None: proc.kill()
            proc.communicate()
            proc.stdout.close(); proc.stderr.close()
        self.stall=None; self.reply=workflow(next_step=1,revision=2)
        self.run_cli(['ui-answer-status','--intent',intent]); self.calls.clear()
        self.assertEqual(self.step(intent,'1','2')[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_parent_fsync_failure_after_step_reply_never_restores_post(self):
        from harness_herdr_ui_receipt import UIReceiptStore
        intent=self.prepare(); path=UIReceiptStore(self.home).root/(intent+'.json')
        self.reply=workflow(next_step=1,revision=2); self.calls.clear(); original=os.fsync
        def fail_after_publish(fd):
            record=json.loads(path.read_text())
            if record.get('last_result',{}).get('revision')==2: raise OSError('SECRET')
            original(fd)
        with patch.object(os,'fsync',side_effect=fail_after_publish):
            code,rows=self.step(intent)
        self.assertEqual(code,1); self.assertNotIn('SECRET',json.dumps(rows))
        self.assertTrue((path.parent/(intent+'.step-ban')).exists())
        self.assertEqual(self.run_cli(['ui-answer-status','--intent',intent])[0],0)
        self.calls.clear(); self.assertEqual(self.step(intent,'1','2')[0],1)
        self.assertFalse(any(r[0]=='POST' for r in self.calls))

    def test_unsafe_marker_cannot_release_even_after_good_reply(self):
        from harness_herdr_ui_receipt import UIReceiptStore
        for kind in ('float','symlink','mode','unlink','missing'):
            self.on_post=None; self.reply=workflow(); intent=self.prepare()
            database=UIReceiptStore(self.home); marker=database.root/(intent+'.step-ban')
            def tamper():
                if kind=='float':
                    value=json.loads(marker.read_text()); value['local_generation']=float(value['local_generation']); marker.write_text(json.dumps(value))
                elif kind=='symlink': marker.unlink(); marker.symlink_to(self.files/'answer')
                elif kind=='mode': marker.chmod(0o644)
                elif kind=='missing': marker.unlink()
            self.on_post=tamper; self.reply=workflow(next_step=1,revision=2)
            if kind=='unlink':
                actual=os.unlink
                def unlink(name,*args,**kwargs):
                    if name==intent+'.step-ban': raise OSError('SECRET')
                    return actual(name,*args,**kwargs)
                with patch.object(os,'unlink',side_effect=unlink): code,rows=self.step(intent)
            else: code,rows=self.step(intent)
            self.assertEqual(code,1,kind); self.assertNotIn('SECRET',json.dumps(rows))
            self.on_post=None; self.run_cli(['ui-answer-status','--intent',intent]); self.calls.clear()
            self.assertEqual(self.step(intent,'1','2')[0],1,kind)
            self.assertFalse(any(r[0]=='POST' for r in self.calls))


def vars_to_args(args):
    return ['ui-answer-prepare','--device',args.device,'--target-file',args.target_file,'--prompt-file',args.prompt_file,'--answer-file',args.answer_file,'--new-workflow']

if __name__ == '__main__': unittest.main()
