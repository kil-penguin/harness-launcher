"""Lifecycle consumer: actual main, private receipts and real loopback only."""
import copy
import io
import json
import os
import signal
import subprocess
import stat
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'bin'))
import harness_herdr_cli as cli
import harness_herdr_lifecycle as lifecycle
import harness_herdr_lifecycle_store as storage
LOCAL='11111111-1111-4111-8111-111111111111'
REMOTE='22222222-2222-4222-8222-222222222222'
OP='33333333-3333-4333-8333-333333333333'
OTHER='44444444-4444-4444-8444-444444444444'
CREATE='lifecycle.guarded_create.v1'
RECEIPT={'receipt_id':'receipt','generation':(1<<63)-1,'resource_ids':{'workspace_id':'w','tab_id':'t','pane_id':'p','terminal_id':'term'}}
def operation(method=CREATE,state='completed',**more):
    value={'operation_id':OP,'server_epoch':'epoch','method':method,'state':state}
    if state=='completed':
        value['lifecycle_effect']={'kind':method.split('_')[-1].split('.')[0],'outcome':'resource_created' if method==CREATE else 'signal_accepted' if 'interrupt' in method else 'resource_closed'}
        if method==CREATE:value['create_receipt']=copy.deepcopy(RECEIPT)
    return {**value,**more}

class LifecycleTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp');self.addCleanup(temp.cleanup)
        self.home=Path(temp.name).resolve();self.home.chmod(0o700)
        self.cwd=self.home/'cwd';self.cwd.write_text('/private/tmp/SECRET_CWD');self.cwd.chmod(0o600)
        self.calls=[];self.reply=operation();self.prepare_reply={'operation_id':OP,'server_epoch':'epoch','admission_ticket':'dGlja2V0','deadline_monotonic_ms':(1<<64)-1};self.epoch='epoch';self.on_post=None;self.failure=None;self.stall=None;self.stalled=threading.Event();self.closed=threading.Event()
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                outer.calls.append(('GET',self.path,dict(self.headers),b''))
                if self.path.endswith('/identity'):value={'protocol':1,'device_id':LOCAL,'native':{'version':'fixture'},'capabilities':{'read_metadata_v1':True,'guarded_mutation_v1':False}}
                else:value={'protocol':1,'device_id':REMOTE if '/machines/' in self.path else LOCAL,'server_epoch':outer.epoch,'workspaces':[],'tabs':[],'panes':[]}
                self.send(value)
            def do_POST(self):
                raw=self.rfile.read(int(self.headers.get('Content-Length','0')));outer.calls.append(('POST',self.path,dict(self.headers),raw))
                if outer.on_post:outer.on_post(self.path,json.loads(raw))
                if outer.stall and self.path.endswith('/create'):
                    if outer.stall=='body':
                        self.send_response(200);self.send_header('Content-Length','200');self.end_headers();self.wfile.write(b'{');self.wfile.flush()
                    outer.stalled.set();self.connection.settimeout(4)
                    try:
                        if self.connection.recv(1)==b'':outer.closed.set()
                    except ConnectionResetError:outer.closed.set()
                    except OSError:pass
                    return
                if outer.failure:self.send(outer.failure[1],outer.failure[0]);return
                self.send(outer.prepare_reply if self.path.endswith('/prepare') else outer.reply)
            def send(self,value,status=200):
                if value is None:self.close_connection=True;return
                raw=value if isinstance(value,bytes) else json.dumps(value).encode()
                self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True).start()
        self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close()))
        self.client=cli.local.Client('a'*64,'plugin',self.server.server_address[1])
    def run_cli(self,args,stream=None):
        out=io.StringIO() if stream is None else stream
        with patch.object(cli.local,'discover',return_value=self.client),patch.object(Path,'home',return_value=self.home),patch('sys.stdout',out),patch('sys.stderr',io.StringIO()):
            try:code=cli.main(args)
            except SystemExit as error:code=error.code
        return code,[json.loads(row) for row in out.getvalue().splitlines()]
    def create(self,device=LOCAL):return self.run_cli(['owned-create','--device',device,'--cwd-file',str(self.cwd),'--new-operation'])
    def db(self):return storage.LifecycleStore(self.home)
    def posts(self):return [r for r in self.calls if r[0]=='POST']
    def test_actual_main_local_remote_prepare_dispatch_and_safe_output(self):
        for device in (LOCAL,REMOTE):
            code,rows=self.create(device);self.assertEqual(code,0,rows)
            self.assertEqual(set(rows[0]),{'intent','device_id','method'});self.assertEqual(set(rows[1]),{'intent','device_id','method','operation'})
            prefix='/api/cli/v1' if device==LOCAL else f'/api/machines/{device}/cli/v1'
            self.assertEqual([r[1] for r in self.posts()],[prefix+'/lifecycle/prepare',prefix+'/lifecycle/create'])
            prepare=json.loads(self.posts()[0][3]);dispatch=json.loads(self.posts()[1][3]);self.assertEqual(dispatch,{**prepare['body'],'admission_ticket':'dGlja2V0'})
            self.assertEqual(len(prepare['body']['owner_key']),43);self.assertNotIn('operation_id',dispatch)
            self.assertNotIn('SECRET',json.dumps(rows));self.assertNotIn(prepare['body']['owner_key'],json.dumps(rows))
            if device==REMOTE:self.assertTrue(all(r[2].get('X-Herdr-Machine')=='1' for r in self.posts()))
            record=self.db().read(rows[0]['intent']);self.assertEqual(record['create_receipt'],RECEIPT);self.assertNotIn('cwd',record)
            self.calls.clear()
    def test_existing_common_0755_parents_keep_modes_and_private_receipts(self):
        (self.home/'.local/state').mkdir(parents=True)
        for path in (self.home/'.local',self.home/'.local/state'):path.chmod(0o755)
        code,rows=self.create();self.assertEqual(code,0,rows)
        for path in (self.home/'.local',self.home/'.local/state'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o755)
        for path in (self.home/'.local/state/harness-herdr-web',self.db().root.parent,self.db().root):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o700)
        self.assertEqual(self.db().read(rows[0]['intent'])['create_receipt'],RECEIPT)
    def test_unsafe_common_parents_and_nonprivate_receipt_dirs_stop_before_http(self):
        for relative,mode in (('.local',0o775),('.local/state',0o777),('.local/state/harness-herdr-web',0o755)):
            with self.subTest(relative=relative):
                path=self.home/relative;path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
                code,rows=self.create();self.assertEqual(code,1,rows);self.assertEqual(self.posts(),[])
                path.chmod(0o700)
        path=self.home/'.local/state';moved=path.with_name('owned-state');path.rename(moved);path.symlink_to(moved,target_is_directory=True)
        code,rows=self.create();self.assertEqual(code,1,rows);self.assertEqual(self.posts(),[])
        path.unlink();moved.rename(path)
    def test_completed_receipt_authorizes_fresh_interrupt_and_exact_singleton_close(self):
        code,rows=self.create();self.assertEqual(code,0,rows);source=rows[0]['intent'];original=self.db().read(source)
        for command,method in [('owned-interrupt','lifecycle.guarded_interrupt.v1'),('owned-close','lifecycle.guarded_close.v1')]:
            self.calls.clear();self.reply=operation(method)
            code,rows=self.run_cli([command,'--receipt',source,'--new-operation']);self.assertEqual(code,0,rows);self.assertNotEqual(rows[0]['intent'],source)
            body=json.loads(self.posts()[0][3])['body'];self.assertEqual(body['owner_key'],original['owner_key']);self.assertEqual(body['receipt'],{'receipt_id':'receipt','generation':(1<<63)-1})
            if command=='owned-close':self.assertEqual(body['closure'],{'members':[RECEIPT['resource_ids']]})
            self.assertEqual(self.db().read(source),original)
    def test_lost_prepare_unknown_network_zero_recovery(self):
        self.prepare_reply=None;code,rows=self.create();self.assertEqual(code,1);intent=rows[0]['intent'];self.assertEqual(len(self.posts()),1);self.calls.clear()
        for suffix in ('status','cancel'):
            self.assertEqual(self.run_cli(['lifecycle-'+suffix,'--intent',intent]),(1,[{'error':{'code':'lifecycle_operation_unknown'}}]))
        self.assertEqual(self.calls,[])
    def test_dispatch_loss_recovers_same_receipt_get_only(self):
        self.reply=None;code,rows=self.create();self.assertEqual(code,1);intent=rows[0]['intent'];self.assertEqual(len(self.posts()),2)
        self.reply=operation();self.calls.clear();code,rows=self.run_cli(['lifecycle-status','--intent',intent]);self.assertEqual(code,0,rows)
        self.assertEqual([r[1].split('/lifecycle/')[-1] for r in self.posts()],['operation']);self.assertEqual(self.db().read(intent)['create_receipt'],RECEIPT)
    def test_wrong_operation_method_epoch_never_authorizes_receipt(self):
        for changes in ({'operation_id':OTHER},{'method':'lifecycle.guarded_close.v1'},{'server_epoch':'restart'}):
            self.reply=operation(**changes);code,rows=self.create();self.assertEqual(code,1,rows);intent=rows[0]['intent'];record=self.db().read(intent);self.assertNotIn('create_receipt',record);self.assertEqual(record['operation_uuid'],OP)
            self.calls.clear();self.assertEqual(self.run_cli(['owned-close','--receipt',intent,'--new-operation'])[0],1);self.assertEqual(self.calls,[])
    def test_bad_options_and_private_cwd_prevent_http(self):
        for args in (['owned-create','--device','local','--cwd-file',str(self.cwd),'--new-operation'],['owned-create','--device',LOCAL,'--cwd-file',str(self.cwd),'--argv','SECRET','--new-operation'],['owned-close','--receipt',LOCAL],['lifecycle-status','--intent','short']):self.assertNotEqual(self.run_cli(args)[0],0)
        for text in ('relative','/x\x00y','/'+ 'x'*4096):
            self.cwd.write_text(text);self.assertEqual(self.create()[0],1)
        self.assertEqual(self.calls,[])
    def test_snapshot_null_or_changed_refuses_prepare(self):
        for epoch in (None,''):
            self.epoch=epoch;self.assertEqual(self.create()[0],1)
        self.assertEqual(self.posts(),[])
    def test_duplicate_oversized_secret_response_not_projected(self):
        for raw in (b'{"operation_id":"SECRET","operation_id":"SECRET"}',b'X'*65537,b'{"error":"SECRET"}',b'\xff'):
            self.reply=raw;code,rows=self.create();self.assertEqual(code,1);self.assertNotIn('SECRET',json.dumps(rows))
    def test_start_flushed_and_dispatch_reserved_before_each_post(self):
        class Output(io.StringIO):
            flushed=''
            def flush(inner):inner.flushed=inner.getvalue()
        output=Output()
        def inspect(path,body):
            rows=[json.loads(r) for r in output.flushed.splitlines()];self.assertEqual(len(rows),1)
            record=self.db().read(rows[0]['intent']);self.assertEqual(record['phase'],'prepare_started' if path.endswith('prepare') else 'dispatch_started')
        self.on_post=inspect;self.assertEqual(self.run_cli(['owned-create','--device',LOCAL,'--cwd-file',str(self.cwd),'--new-operation'],output)[0],0)
    def test_output_flush_failure_prevents_prepare(self):
        class Output(io.StringIO):
            def flush(self):raise OSError('SECRET')
        self.assertEqual(self.run_cli(['owned-create','--device',LOCAL,'--cwd-file',str(self.cwd),'--new-operation'],Output())[0],1);self.assertEqual(self.posts(),[])
    def test_closed_method_state_effect_matrix_and_cancel_pair(self):
        for method in storage.METHODS:
            for state in ('prepared','queued','reserved','completed','cancelled','rejected'):
                value=operation(method,state)
                if state in {'cancelled','rejected'}:value['reason']='cancelled' if state=='cancelled' else 'guard_mismatch'
                self.assertEqual(storage.operation(value),value)
            for state in ('spawn_claimed','spawned','installing','reconciling'):
                value=operation(method,state)
                if state!='spawn_claimed':value['lifecycle_effect']={'kind':'create','outcome':'spawned'}
                if method==CREATE:self.assertEqual(storage.operation(value),value)
                else:
                    with self.assertRaises(storage.LifecycleStoreError):storage.operation(value)
        for value in (operation(state='spawn_claimed',lifecycle_effect={'kind':'create','outcome':'spawned'}),operation(state='reconciling'),operation(create_receipt={**RECEIPT,'generation':1<<63}),operation(effect={'text_bytes_accepted':0,'enter_accepted':False})):
            with self.assertRaises(storage.LifecycleStoreError):storage.operation(value)
        with self.assertRaises(lifecycle.LifecycleError):lifecycle.checked_cancel({'status':'cancelled','operation':operation()}, {'epoch':'epoch','operation_uuid':OP,'method':CREATE})
    def test_known_queued_reserved_remain_cancellable_but_lost_dispatch_is_get_only(self):
        for state in ('queued','reserved'):
            self.reply=operation(state=state);code,rows=self.create();self.assertEqual(code,0,rows);intent=rows[0]['intent']
            self.reply={'status':'cancelled','operation':operation(state='cancelled',reason='cancelled')}
            code,rows=self.run_cli(['lifecycle-cancel','--intent',intent]);self.assertEqual(code,0,rows)
        self.reply=None;code,rows=self.create();self.assertEqual(code,1);intent=rows[0]['intent']
        self.reply=operation(state='queued');self.run_cli(['lifecycle-status','--intent',intent]);self.calls.clear()
        self.assertEqual(self.run_cli(['lifecycle-cancel','--intent',intent])[0],1);self.assertEqual(self.calls,[])
    def test_prepare_closed_u64_ticket_and_binding_reject_before_dispatch(self):
        original=copy.deepcopy(self.prepare_reply)
        invalid=[{**original,'deadline_monotonic_ms':1<<64},{**original,'deadline_monotonic_ms':True},{**original,'admission_ticket':'AB'},{**original,'server_epoch':'restart'},{**original,'extra':'SECRET'},json.dumps(original).replace(str((1<<64)-1),'-0').encode(),json.dumps(original).replace(str((1<<64)-1),'1e2').encode()]
        for value in invalid:
            self.calls.clear();self.prepare_reply=value;code,rows=self.create();self.assertEqual(code,1,rows);self.assertEqual(len(self.posts()),1);self.assertNotIn('SECRET',json.dumps(rows))
    def test_six_transport_routes_are_exact_and_never_get_or_query_aliases(self):
        for suffix in ('prepare','create','interrupt','close','operation','operation/cancel'):
            route='/api/cli/v1/lifecycle/'+suffix
            for method,path in (('GET',route),('POST',route+'/'),('POST',route+'?x=1'),('POST',route+'x')):
                with self.assertRaises(cli.local.TransportError):self.client.lifecycle_json(method,path,{})
        self.assertEqual(self.calls,[])
    def test_store_lexically_noncanonical_and_secret_extra_never_reads(self):
        code,rows=self.create();self.assertEqual(code,0,rows);db=self.db();intent=rows[0]['intent'];path=db.root/(intent+'.json');raw=path.read_bytes()
        for value in (raw.replace(b'"version":1',b'"version":-0'),raw.replace(('"revision":'+str(json.loads(raw)['revision'])).encode(),b'"revision":-0'),raw[:-1]+b',"secret":"SECRET"}',b'{"version":1,"version":1}',b'X'*65537,b'{"x":'+b'['*101+b'0'+b']'*101+b'}'):
            if value==raw:continue
            path.write_bytes(value)
            with self.assertRaises((storage.LifecycleStoreError,cli.store.StoreError)):db.read(intent)
        path.write_bytes(raw)
    def test_prepared_operation_binding_never_changes_on_later_cas(self):
        code,rows=self.create();self.assertEqual(code,0,rows);db=self.db();record=db.read(rows[0]['intent'])
        with self.assertRaises(storage.LifecycleStoreError):db.transition(record,phase=record['phase'],prepared={**self.prepare_reply,'operation_id':OTHER})
        self.assertEqual(db.read(record['intent']),record)
    def test_private_cwd_and_create_proof_read_once_across_prepare(self):
        original=self.cwd.read_text()
        self.on_post=lambda path,body:self.cwd.write_text('/replacement') if path.endswith('/prepare') else None
        code,rows=self.create();self.assertEqual(code,0,rows);self.assertEqual(json.loads(self.posts()[1][3])['spec']['cwd'],original)
        source=rows[0]['intent'];source_record=self.db().read(source);self.calls.clear();self.reply=operation('lifecycle.guarded_close.v1')
        def replace_source(path,body):
            if path.endswith('/prepare'):
                changed=copy.deepcopy(source_record);changed['owner_key']='B'*43;changed['create_receipt']['resource_ids']['pane_id']='replacement'
                (self.db().root/(source+'.json')).write_text(json.dumps(changed))
        self.on_post=replace_source;code,rows=self.run_cli(['owned-close','--receipt',source,'--new-operation']);self.assertEqual(code,0,rows)
        body=json.loads(self.posts()[1][3]);self.assertEqual(body['owner_key'],source_record['owner_key']);self.assertEqual(body['closure']['members'],[RECEIPT['resource_ids']])
    def test_concurrent_cas_has_one_winner_and_u63_revision_refuses_overflow(self):
        code,rows=self.create();self.assertEqual(code,0,rows);db=self.db();record=db.read(rows[0]['intent']);barrier=threading.Barrier(2);winners=[]
        def worker():
            barrier.wait()
            try:db.transition(record,phase=record['phase']);winners.append(True)
            except (storage.LifecycleStoreError,cli.store.StoreError):winners.append(False)
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sorted(winners),[False,True]);record=db.read(record['intent']);record['revision']=(1<<63)-1;path=db.root/(record['intent']+'.json');path.write_text(json.dumps(record))
        with self.assertRaises(storage.LifecycleStoreError):db.transition(record,phase=record['phase'])
        self.assertEqual(db.read(record['intent']),record)
    def test_store_symlink_hardlink_and_parent_substitution_fail_closed(self):
        code,rows=self.create();self.assertEqual(code,0,rows);db=self.db();intent=rows[0]['intent'];path=db.root/(intent+'.json');raw=path.read_bytes()
        link=db.root/'linked';os.link(path,link)
        with self.assertRaises(storage.LifecycleStoreError):db.read(intent)
        link.unlink();path.unlink();path.symlink_to(self.cwd)
        with self.assertRaises((OSError,cli.store.StoreError)):db.read(intent)
        path.unlink();path.write_bytes(raw);path.chmod(0o600);moved=db.root.with_name('moved');db.root.rename(moved);db.root.symlink_to(self.home,target_is_directory=True)
        with self.assertRaises((OSError,cli.store.StoreError)):db.read(intent)
    def child(self,extra=''):
        script="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import harness_herdr_cli as cli;cli.local.discover=lambda:cli.local.Client('a'*64,'plugin',int(sys.argv[2]));"+extra+"sys.exit(cli.main(sys.argv[3:]))"
        env={**os.environ,'HOME':str(self.home)}
        return subprocess.Popen([sys.executable,'-c',script,str(Path(cli.__file__).parent),str(self.server.server_address[1]),'owned-create','--device',LOCAL,'--cwd-file',str(self.cwd),'--new-operation'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    def test_ctrl_c_closes_effect_request_socket_without_replay(self):
        self.stall='body';child=self.child();self.addCleanup(lambda:(child.kill(),child.communicate()) if child.poll() is None else None)
        self.assertTrue(self.stalled.wait(3));child.send_signal(signal.SIGINT);out,err=child.communicate(timeout=4)
        self.assertEqual(child.returncode,1,err);self.assertTrue(self.closed.wait(1));self.assertNotIn('SECRET',out+err);self.assertEqual(len(self.posts()),2)
        intent=json.loads(out.splitlines()[0])['intent'];self.assertEqual(self.db().read(intent)['phase'],'dispatch_started')
    def test_absolute_timeout_headers_and_body_close_socket_once(self):
        for stage in ('headers','body'):
            self.stall=stage;self.stalled.clear();self.closed.clear();self.calls.clear()
            with patch.object(cli.local,'REQUEST_DEADLINE_SECONDS',.04):code,rows=self.create()
            self.assertEqual(code,1,rows);self.assertEqual(rows[-1]['error']['code'],'deadline_exceeded');self.assertTrue(self.closed.wait(1));self.assertEqual(len(self.posts()),2)
    def test_positive_create_parent_fsync_failure_needs_same_status_durable_recovery(self):
        original=os.fsync
        def failed(fd):
            files=list((self.home/'.local/state/harness-herdr-web/lifecycle-receipts/v1').glob('*.json'))
            if files and json.loads(files[0].read_text())['phase']=='terminal_observed':raise OSError('SECRET')
            return original(fd)
        with patch.object(os,'fsync',side_effect=failed):code,rows=self.create()
        self.assertEqual(code,1,rows);intent=rows[0]['intent'];self.assertEqual(len(self.posts()),2);self.calls.clear()
        self.assertEqual(self.run_cli(['owned-close','--receipt',intent,'--new-operation'])[0],1);self.assertEqual(self.calls,[])
        self.assertEqual(self.run_cli(['lifecycle-status','--intent',intent])[0],0);self.assertEqual([r[1].split('/lifecycle/')[-1] for r in self.posts()],['operation'])
        self.reply=operation('lifecycle.guarded_close.v1');self.assertEqual(self.run_cli(['owned-close','--receipt',intent,'--new-operation'])[0],0)
    def test_get_binding_mismatch_and_cancel_loss_never_revive_cancel(self):
        for loss in ('get','cancel'):
            self.reply=operation(state='queued');code,rows=self.create();self.assertEqual(code,0,rows);intent=rows[0]['intent']
            self.reply=operation(state='queued',operation_id=OTHER) if loss=='get' else None
            self.assertEqual(self.run_cli(['lifecycle-status' if loss=='get' else 'lifecycle-cancel','--intent',intent])[0],1)
            self.reply=operation(state='queued');self.assertEqual(self.run_cli(['lifecycle-status','--intent',intent])[0],0);self.calls.clear()
            self.assertEqual(self.run_cli(['lifecycle-cancel','--intent',intent])[0],1);self.assertEqual(self.calls,[])
    def test_each_write_fsync_and_flush_boundary_prevents_later_post(self):
        original=storage._Files._write
        for phase,after,expected in [('created',False,0),('created',True,0),('prepare_started',False,0),('prepare_started',True,0),('prepared',False,1),('prepared',True,1),('dispatch_started',False,1),('dispatch_started',True,1),('terminal_observed',False,2),('terminal_observed',True,2)]:
            self.calls.clear()
            def failed(instance,root,value):
                if value['phase']==phase and not after:raise OSError('SECRET')
                original(instance,root,value)
                if value['phase']==phase and after:raise OSError('SECRET')
            with patch.object(storage._Files,'_write',new=failed):code,rows=self.create()
            self.assertEqual(code,1,(phase,after,rows));self.assertEqual(len(self.posts()),expected,(phase,after));self.assertNotIn('SECRET',json.dumps(rows))
        class Output(io.StringIO):
            count=0
            def flush(inner):
                inner.count+=1
                if inner.count==2:raise OSError('SECRET')
        self.calls.clear();self.assertEqual(self.run_cli(['owned-create','--device',LOCAL,'--cwd-file',str(self.cwd),'--new-operation'],Output())[0],1);self.assertEqual(len(self.posts()),1)
    def test_marker_modes_links_tuple_and_inode_replacement_never_release_authority(self):
        self.reply=None;code,rows=self.create();self.assertEqual(code,1);intent=rows[0]['intent'];db=self.db();record=db.read(intent)
        self.reply=operation();marker=db.root/(intent+'.recovery');original=marker.read_bytes()
        variants=('mode','link','symlink','tuple','revision')
        for variant in variants:
            marker.unlink();marker.write_bytes(original);marker.chmod(0o600);linked=db.root/'marker-linked'
            if variant=='mode':marker.chmod(0o644)
            if variant=='link':os.link(marker,linked)
            if variant=='symlink':marker.unlink();marker.symlink_to(self.cwd)
            if variant in {'tuple','revision'}:
                value=json.loads(original);value['operation_uuid' if variant=='tuple' else 'revision']=OTHER if variant=='tuple' else record['revision']+100;marker.write_text(json.dumps(value))
            code,rows=self.run_cli(['lifecycle-status','--intent',intent]);self.assertEqual(code,1,(variant,rows));self.assertTrue(marker.is_symlink() or marker.exists())
            self.calls.clear();self.assertEqual(self.run_cli(['owned-close','--receipt',intent,'--new-operation'])[0],1);self.assertEqual(self.calls,[])
            if linked.exists():linked.unlink()
        marker.unlink();marker.write_bytes(original);marker.chmod(0o600)
        original_remove=storage.LifecycleStore._remove_marker
        def replaced(instance,root,value,kind,token):
            if kind=='recovery':
                old=instance.root/(intent+'.recovery');replacement=instance.root/'replacement';replacement.write_bytes(old.read_bytes());replacement.chmod(0o600);os.replace(replacement,old)
            return original_remove(instance,root,value,kind,token)
        with patch.object(storage.LifecycleStore,'_remove_marker',new=replaced):self.assertEqual(self.run_cli(['lifecycle-status','--intent',intent])[0],1)
        self.assertTrue(marker.exists());self.calls.clear();self.assertEqual(self.run_cli(['owned-close','--receipt',intent,'--new-operation'])[0],1);self.assertEqual(self.calls,[])
    def test_get_only_marker_unsafe_or_replaced_never_allows_cancel(self):
        for variant in ('mode','link','symlink','content'):
            self.reply=None;code,rows=self.create();self.assertEqual(code,1);intent=rows[0]['intent'];db=self.db();marker=db.root/(intent+'.get-only')
            if variant=='mode':marker.chmod(0o644)
            if variant=='link':os.link(marker,db.root/(intent+'.hardlink'))
            if variant=='symlink':marker.unlink();marker.symlink_to(self.cwd)
            if variant=='content':marker.write_text('{"SECRET":true}')
            self.calls.clear();self.assertEqual(self.run_cli(['lifecycle-cancel','--intent',intent])[0],1);self.assertEqual(self.calls,[])
    def test_real_process_crash_boundaries_never_dispatch_again(self):
        for phase,expected in [('created',0),('prepare_started',0),('prepared',1),('dispatch_started',1),('terminal_observed',2)]:
            self.calls.clear()
            extra="import os;original=cli.lifecycle_store._Files._write\ndef crash(instance,root,value):\n original(instance,root,value)\n if value['phase']=="+repr(phase)+":os._exit(77)\ncli.lifecycle_store._Files._write=crash\n"
            child=self.child(extra);out,err=child.communicate(timeout=4);self.assertEqual(child.returncode,77,err);self.assertEqual(len(self.posts()),expected)
            records=list(self.db().root.glob('*.json'));latest=max(records,key=lambda p:p.stat().st_mtime_ns);record=json.loads(latest.read_text());self.calls.clear()
            code,rows=self.run_cli(['lifecycle-status','--intent',record['intent']])
            self.assertTrue(all('/operation' in r[1] or r[0]=='GET' for r in self.calls));self.assertFalse(any(r[1].endswith('/create') or r[1].endswith('/prepare') for r in self.posts()))
    def test_marker_file_parent_fsync_failures_stop_at_prepare_and_unlink_failure_stays_blocked(self):
        actual=os.fsync
        for kind,directory in [('get-only',False),('get-only',True),('recovery',False),('recovery',True)]:
            self.calls.clear();self.reply=operation();armed=[False]
            self.on_post=lambda path,body:armed.__setitem__(0,True) if path.endswith('/prepare') else None
            def failed(fd):
                root=self.home/'.local/state/harness-herdr-web/lifecycle-receipts/v1'
                records=list(root.glob('*.json'))
                if records:
                    latest=max(records,key=lambda path:path.stat().st_mtime_ns);record=json.loads(latest.read_text());marker=root/(record['intent']+'.'+kind)
                    if armed[0] and marker.exists() and record['phase']=='prepared' and stat.S_ISDIR(os.fstat(fd).st_mode)==directory:raise OSError('SECRET')
                return actual(fd)
            with patch.object(os,'fsync',side_effect=failed):code,rows=self.create()
            self.assertEqual(code,1,(kind,directory,rows));self.assertEqual(len(self.posts()),1)
        self.on_post=None
        actual_unlink=os.unlink
        def failed_unlink(path,*args,**kwargs):
            if str(path).endswith('.recovery'):raise OSError('SECRET')
            return actual_unlink(path,*args,**kwargs)
        self.calls.clear()
        with patch.object(os,'unlink',side_effect=failed_unlink):code,rows=self.create()
        self.assertEqual(code,1);intent=rows[0]['intent'];self.calls.clear();self.assertEqual(self.run_cli(['owned-close','--receipt',intent,'--new-operation'])[0],1);self.assertEqual(self.calls,[])
        self.assertEqual(self.run_cli(['lifecycle-status','--intent',intent])[0],0)
    def replace_recovery_during_post(self,path,body):
        if not (path.endswith('/create') or path.endswith('/operation')):return
        marker=next(self.db().root.glob('*.recovery'));before=marker.stat();raw=marker.read_bytes()
        replacement=marker.with_suffix('.replacement');replacement.write_bytes(raw);replacement.chmod(0o600);os.replace(replacement,marker)
        self.assertEqual(marker.read_bytes(),raw);self.assertNotEqual(marker.stat().st_ino,before.st_ino)
    def assert_replaced_recovery_has_no_authority(self,intent,code,rows):
        self.assertEqual(code,1,rows);db=self.db();record=db.read(intent)
        self.assertEqual(record['phase'],'dispatch_started');self.assertNotIn('create_receipt',record)
        self.assertTrue((db.root/(intent+'.recovery')).exists());self.assertTrue((db.root/(intent+'.get-only')).exists())
        self.calls.clear();self.on_post=None;self.reply=operation('lifecycle.guarded_close.v1')
        code,rows=self.run_cli(['owned-close','--receipt',intent,'--new-operation']);self.assertEqual(code,1,rows);self.assertEqual(self.calls,[])
    def test_recovery_original_inode_pinned_before_dispatch_network(self):
        self.on_post=self.replace_recovery_during_post;code,rows=self.create();intent=rows[0]['intent']
        self.assertEqual(len(self.posts()),2)
        self.assert_replaced_recovery_has_no_authority(intent,code,rows)
    def test_recovery_original_inode_pinned_before_status_network(self):
        self.reply=None;code,rows=self.create();self.assertEqual(code,1);intent=rows[0]['intent']
        self.calls.clear();self.reply=operation();self.on_post=self.replace_recovery_during_post
        code,rows=self.run_cli(['lifecycle-status','--intent',intent]);self.assertEqual([row[1].split('/lifecycle/')[-1] for row in self.posts()],['operation'])
        self.assert_replaced_recovery_has_no_authority(intent,code,rows)
    def test_replaced_original_get_only_marker_refuses_observation_and_receipt_commit(self):
        def replace(path,body):
            if not path.endswith('/create'):return
            db=self.db();markers=list(db.root.glob('*.get-only'));marker=markers[-1];replacement=db.root/'replacement';replacement.write_bytes(marker.read_bytes());replacement.chmod(0o600);os.replace(replacement,marker)
        self.on_post=replace;code,rows=self.create();self.assertEqual(code,1);intent=rows[0]['intent'];self.assertNotIn('create_receipt',self.db().read(intent));self.assertTrue((self.db().root/(intent+'.get-only')).exists())
    def test_unknown_error_code_with_null_message_never_escapes(self):
        for status in (400,403,409,429,502):
            self.assertEqual(cli.local.lifecycle_error_code(status,{'error':{'code':'SECRET','message':None}}),'guarded_native_unavailable')
    def test_native_and_transport_errors_have_exact_closed_message_mapping(self):
        pairs=[(400,'lifecycle_invalid_request','Invalid lifecycle request'),(429,'lifecycle_admission_full','Lifecycle admission is full'),(409,'lifecycle_operation_unknown','Lifecycle operation is unknown'),(409,'guard_not_supported','Guarded input is not supported'),(502,'guarded_native_unavailable','Guarded Native is unavailable'),(403,'revoked','Drive access is unavailable'),(403,'read_only','This device can only watch'),(404,'not_found','not found')]
        for status,code,message in pairs:
            self.assertEqual(cli.local.lifecycle_error_code(status,{'error':{'code':code,'message':message}}),code)
            self.assertEqual(cli.local.lifecycle_error_code(status,{'error':{'code':code,'message':'SECRET'}}),'guarded_native_unavailable')
            self.assertEqual(cli.local.lifecycle_error_code(status,{'error':{'code':code,'message':message,'data':'SECRET'}}),'guarded_native_unavailable')
    def test_get_cancel_all_binding_dimensions_reject_without_receipt_or_terminal_transition(self):
        for command in ('lifecycle-status','lifecycle-cancel'):
            for result in (operation(state='queued',operation_id=OTHER),operation('lifecycle.guarded_close.v1'),operation(server_epoch='restart')):
                self.reply=operation(state='queued');code,rows=self.create();self.assertEqual(code,0,rows);intent=rows[0]['intent']
                self.reply=result if command=='lifecycle-status' else {'status':'too_late','operation':result}
                code,rows=self.run_cli([command,'--intent',intent]);self.assertEqual(code,1,rows);record=self.db().read(intent)
                self.assertEqual(record['phase'],'dispatch_started');self.assertEqual(record['operation_uuid'],OP);self.assertEqual(record['method'],CREATE);self.assertNotIn('create_receipt',record)
                self.reply=operation(state='queued');self.assertEqual(self.run_cli(['lifecycle-status','--intent',intent])[0],0);self.calls.clear();self.assertEqual(self.run_cli(['lifecycle-cancel','--intent',intent])[0],1);self.assertEqual(self.calls,[])
    def test_too_late_spawn_claimed_keeps_effect_absent(self):
        self.reply=operation(state='queued');code,rows=self.create();self.assertEqual(code,0,rows);intent=rows[0]['intent']
        self.reply={'status':'too_late','operation':operation(state='spawn_claimed')};code,rows=self.run_cli(['lifecycle-cancel','--intent',intent]);self.assertEqual(code,0,rows);self.assertNotIn('lifecycle_effect',rows[-1]['operation'])
    def test_http_unknown_revoked_and_secret_error_fail_closed_without_adoption(self):
        for status,code,message in [(409,'lifecycle_operation_unknown','Lifecycle operation is unknown'),(403,'revoked','Drive access is unavailable'),(502,'guarded_native_unavailable','SECRET')]:
            self.reply=operation(state='queued');code0,rows=self.create();self.assertEqual(code0,0,rows);intent=rows[0]['intent'];self.calls.clear()
            self.failure=(status,{'error':{'code':code,'message':message}});code0,rows=self.run_cli(['lifecycle-status','--intent',intent]);self.assertEqual(code0,1,rows);self.assertNotIn('SECRET',json.dumps(rows));self.assertEqual(rows[-1]['error']['code'],code);self.assertEqual(len(self.posts()),1)
            self.failure=None;self.calls.clear();self.assertEqual(self.run_cli(['owned-close','--receipt',intent,'--new-operation'])[0],1);self.assertEqual(self.calls,[])
    def test_private_store_cas_modes_and_immutable_binding(self):
        code,rows=self.create();self.assertEqual(code,0,rows);db=self.db();record=db.read(rows[0]['intent']);path=db.root/(record['intent']+'.json')
        self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(db.root.stat().st_mode&0o777,0o700)
        next_record=db.transition(record,phase=record['phase'])
        with self.assertRaises(storage.LifecycleStoreError):db.transition(record,phase=record['phase'])
        changed=copy.deepcopy(next_record);changed['device_uuid']=REMOTE
        with self.assertRaises(storage.LifecycleStoreError):db.transition(changed,phase=changed['phase'])
        path.chmod(0o644)
        with self.assertRaises(Exception):db.read(record['intent'])

if __name__=='__main__':unittest.main()
