"""Explicit bounded pane read: actual main and isolated real loopback fixtures."""
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'bin'))
import harness_herdr_cli as cli
LOCAL='11111111-1111-4111-8111-111111111111'
REMOTE='22222222-2222-4222-8222-222222222222'
ROW={'workspace_id':'w','tab_id':'t','pane_id':'p','terminal_id':'term','agent':None,'agent_status':'idle'}
def reply(device=LOCAL,**changes):return {'device_id':device,'server_epoch':'epoch',**{key:ROW[key] for key in ('workspace_id','tab_id','pane_id','terminal_id')},'text':'hello','truncated':False,**changes}
class ReadTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp');self.addCleanup(temp.cleanup);self.home=Path(temp.name).resolve();self.home.chmod(0o700)
        self.calls=[];self.rows=[copy.deepcopy(ROW)];self.epoch='epoch';self.reply=reply();self.failure=None;self.stall=None;self.stalled=threading.Event();self.closed=threading.Event();self.delays={}
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                outer.calls.append(('GET',self.path,dict(self.headers),self.rfile.read(int(self.headers.get('Content-Length','0')))))
                if self.path.endswith('/identity'):value={'protocol':1,'device_id':LOCAL,'native':{'version':'fixture'},'capabilities':{'read_metadata_v1':True,'guarded_mutation_v1':False}}
                else:value={'protocol':1,'device_id':REMOTE if '/machines/' in self.path else LOCAL,'server_epoch':outer.epoch,'workspaces':[{'workspace_id':'w'}],'tabs':[{'workspace_id':'w','tab_id':'t'}],'panes':outer.rows,'revision':(1<<64)-1}
                self.send(value)
            def do_POST(self):
                raw=self.rfile.read(int(self.headers.get('Content-Length','0')));outer.calls.append(('POST',self.path,dict(self.headers),raw));self.send(outer.failure[1] if outer.failure else outer.reply,outer.failure[0] if outer.failure else 200)
            def send(self,value,status=200):
                if outer.stall and outer.stall[0] in self.path:
                    if outer.stall[1]=='body':self.send_response(200);self.send_header('Content-Length','200');self.end_headers();self.wfile.write(b'{');self.wfile.flush()
                    outer.stalled.set();self.connection.settimeout(4)
                    try:
                        if self.connection.recv(1)==b'':outer.closed.set()
                    except ConnectionResetError:outer.closed.set()
                    except OSError:pass
                    return
                for suffix,delay in outer.delays.items():
                    if self.path.endswith(suffix):time.sleep(delay)
                if value is None:self.close_connection=True;return
                raw=value if isinstance(value,bytes) else json.dumps(value).encode()
                try:self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True).start();self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close()))
        self.client=cli.local.Client('a'*64,'plugin',self.server.server_address[1]);self.raw=''
    def run_cli(self,options=(),device=LOCAL):
        out=io.StringIO()
        with patch.object(cli.local,'discover',return_value=self.client),patch.object(Path,'home',return_value=self.home),patch('sys.stdout',out),patch('sys.stderr',io.StringIO()):
            try:code=cli.main(['read','--device',device,'--pane','p',*options])
            except SystemExit as error:code=error.code
        self.raw=out.getvalue();return code,[json.loads(row) for row in self.raw.splitlines()]
    def posts(self):return [row for row in self.calls if row[0]=='POST']
    def test_default_read_returns_selected_metadata_row_only_and_body_calls_zero(self):
        self.rows[0].update(text='SECRET',session='SECRET',revision=(1<<64)-1)
        self.assertEqual(self.run_cli(),(0,[ROW]));self.assertEqual(self.posts(),[]);self.assertNotIn('SECRET',self.raw)
    def test_body_opt_in_local_remote_exact_body_headers_and_defaults(self):
        for mode in ('plugin','managed'):
            self.client=cli.local.Client('a'*64,mode,self.server.server_address[1])
            for device in (LOCAL,REMOTE):
                self.calls.clear();self.reply=reply(device);code,rows=self.run_cli(['--body'],device);self.assertEqual((code,rows),(0,[self.reply]))
                prefix='/api/cli/v1' if device==LOCAL else f'/api/machines/{device}/cli/v1';post=self.posts();self.assertEqual(len(post),1);self.assertEqual(post[0][1],prefix+'/pane/read')
                self.assertEqual(json.loads(post[0][3]),{'server_epoch':'epoch',**{key:ROW[key] for key in ('workspace_id','tab_id','pane_id','terminal_id')},'lines':40,'max_bytes':4096})
                if device==REMOTE:self.assertEqual(post[0][2].get('X-Herdr-Machine'),'1')
                self.assertNotIn('X-Herdr-Launcher',post[0][2]);self.assertTrue(all(row[3]==b'' for row in self.calls if row[0]=='GET'))
    def test_lexical_invalid_options_network_zero_before_int_even_5000_digits(self):
        for name,maximum in (('--lines',200),('--max-bytes',8192)):
            for value in ('0','00','-0','-1','+1','1.0','1e2','١','9'*5000,str(maximum+1)):
                self.assertNotEqual(self.run_cli(['--body',name,value])[0],0)
            self.assertNotEqual(self.run_cli([name,'1'])[0],0)
        for options in (['--body','--source','visible'],['--body','--format','text'],['--body','--url','http://SECRET'],['--bo'],['--session','SECRET']):self.assertNotEqual(self.run_cli(options)[0],0)
        self.assertNotEqual(self.run_cli(['--body'],device='local')[0],0);self.assertEqual(self.calls,[])
    def test_unique_current_membership_and_nonnull_epoch_before_body(self):
        for rows,epoch in (([], 'epoch'),([copy.deepcopy(ROW),copy.deepcopy(ROW)],'epoch'),([{**ROW,'terminal_id':None}],'epoch'),([copy.deepcopy(ROW)],None),([copy.deepcopy(ROW)],'')):
            self.calls.clear();self.rows=rows;self.epoch=epoch;self.assertEqual(self.run_cli(['--body'])[0],1);self.assertEqual(self.posts(),[])
    def test_wrong_closed_response_tuple_text_or_flag_never_reaches_stdout(self):
        for changes in ({'device_id':REMOTE},{'server_epoch':'restart'},{'workspace_id':'other'},{'tab_id':'other'},{'pane_id':'other'},{'terminal_id':'other'},{'truncated':0},{'text':None},{'text':'\ud800'},{'session':'SECRET'},{'text':'x'*4097}):
            self.reply=reply(**changes);code,rows=self.run_cli(['--body']);self.assertEqual(code,1,rows);self.assertEqual(len(rows),1);self.assertIn('error',rows[0]);self.assertNotIn('SECRET',self.raw)
        self.assertEqual(len(self.posts()),11)
    def test_unicode_bytes_and_controls_are_preserved_json_escaped_and_bounded(self):
        text='한🙂\x00\x1b[31m\r\n\t';self.reply=reply(text=text,truncated=True)
        code,rows=self.run_cli(['--body','--lines','200','--max-bytes',str(len(text.encode()))]);self.assertEqual((code,rows),(0,[self.reply]));self.assertNotIn('\x1b',self.raw);self.assertNotIn('\x00',self.raw);self.assertEqual(len(self.raw.splitlines()),1)
        self.assertEqual(self.run_cli(['--body','--max-bytes',str(len(text.encode())-1)])[0],1)
        self.reply=reply(text='🙂'*2048);self.assertEqual(self.run_cli(['--body','--max-bytes','8192'])[0],0)
    def test_duplicate_malformed_oversized_deep_and_fatal_utf8_body_no_raw_error(self):
        for value in (b'{"text":"SECRET","text":"SECRET"}',b'X'*65537,b'\xff',b'{"SECRET":'+b'['*101+b'0'+b']'*101+b'}',b'{"text":NaN}',None):
            self.reply=value;code,rows=self.run_cli(['--body']);self.assertEqual(code,1,rows);self.assertNotIn('SECRET',self.raw)
    def test_fixed_errors_unsupported_watch_revoke_never_retry(self):
        for status,code,message,expected in [(404,'SECRET','SECRET','guard_not_supported'),(409,'guard_mismatch','guard target no longer matches','guard_mismatch'),(502,'guarded_read_unavailable','Guarded pane read is unavailable','guarded_read_unavailable'),(400,'invalid_request','Invalid guarded pane read request','invalid_request'),(403,'read_only','This device can only watch','read_only'),(403,'revoked','Drive access is unavailable','revoked'),(502,'SECRET',None,'guarded_read_unavailable')]:
            self.calls.clear();self.failure=(status,{'error':{'code':code,'message':message}});result,rows=self.run_cli(['--body']);self.assertEqual(result,1,rows);self.assertEqual(rows,[{'error':{'code':expected}}]);self.assertEqual(len(self.posts()),1);self.assertNotIn('SECRET',self.raw)
    def test_shared_absolute_deadline_includes_identity_snapshot_and_reply(self):
        from types import SimpleNamespace
        clock = [100.0]
        calls = []
        class Transport:
            def pane_read_json(transport, method, path, body=None, deadline_seconds=None):
                calls.append((method, path, deadline_seconds))
                if deadline_seconds < .03:
                    clock[0] += deadline_seconds
                    raise cli.local.TransportError('deadline_exceeded')
                clock[0] += .03
                if path.endswith('/identity'):
                    return {'protocol':1,'device_id':LOCAL,'native':{'version':'fixture'},'capabilities':{'read_metadata_v1':True,'guarded_mutation_v1':False}}
                if path.endswith('/snapshot'):
                    return {'protocol':1,'device_id':LOCAL,'server_epoch':'epoch','workspaces':[{'workspace_id':'w'}],'tabs':[{'workspace_id':'w','tab_id':'t'}],'panes':[copy.deepcopy(ROW)]}
                return reply()
        self.client = Transport()
        with patch.object(cli.local,'REQUEST_DEADLINE_SECONDS',.075), patch.object(cli.pane_read,'time',SimpleNamespace(monotonic=lambda:clock[0])), patch.object(cli.local,'time',SimpleNamespace(monotonic=lambda:clock[0])):
            code, rows = self.run_cli(['--body'])
        self.assertEqual(code,1,rows)
        self.assertEqual(rows,[{'error':{'code':'deadline_exceeded'}}])
        self.assertEqual([(method,path) for method,path,_ in calls],[('GET','/api/cli/v1/identity'),('GET','/api/cli/v1/snapshot'),('POST','/api/cli/v1/pane/read')])
        for call, expected in zip(calls,(.075,.045,.015)):
            self.assertAlmostEqual(call[2],expected,places=9)
        self.assertAlmostEqual(clock[0]-100.0,.075,places=9)
    def test_timeout_headers_and_body_for_metadata_and_read_close_owned_socket(self):
        for path,stage in (('/identity','headers'),('/snapshot','body'),('/pane/read','headers'),('/pane/read','body')):
            self.calls.clear();self.stalled.clear();self.closed.clear();self.stall=(path,stage)
            with patch.object(cli.local,'REQUEST_DEADLINE_SECONDS',.05):code,rows=self.run_cli(['--body'])
            self.assertEqual(code,1,rows);self.assertEqual(rows,[{'error':{'code':'deadline_exceeded'}}]);self.assertTrue(self.closed.wait(1));self.assertLessEqual(len(self.posts()),1)
    def test_route_allowlist_no_alias_body_on_get_or_arbitrary_url_can_reach_network(self):
        path='/api/cli/v1/pane/read'
        for method,route in (('GET',path),('POST',path+'/'),('POST',path+'?source=visible'),('POST',path+'#fragment'),('POST',path+'x'),('POST','/api/machines/short/cli/v1/pane/read'),('POST','https://SECRET/api/cli/v1/pane/read')):
            with self.assertRaises(cli.local.TransportError):self.client.pane_read_json(method,route,{})
        with self.assertRaises(cli.local.TransportError):self.client.pane_read_json('GET','/api/cli/v1/snapshot',{})
        with self.assertRaises(cli.local.TransportError):self.client.post_json(path+'x',{})
        self.assertEqual(self.calls,[])
    def test_body_response_never_exposes_private_proof_target_or_raw_headers(self):
        for extra in ('target','owner_key','admission_ticket','session','source','format','revision'):
            self.reply=reply(**{extra:'SECRET'});self.assertEqual(self.run_cli(['--body'])[0],1);self.assertNotIn('SECRET',self.raw)
        self.rows[0].update(owner_key='SECRET',agent_binding={'session':'SECRET'});self.reply=reply();code,rows=self.run_cli(['--body']);self.assertEqual(code,0,rows);self.assertNotIn('SECRET',self.raw)
    def test_guard_ids_and_invalid_epoch_refuse_body_before_network_or_dispatch(self):
        for pane in ('','p\x00','p\x1b','x'*257):
            self.assertNotEqual(self.run_cli(['--body','--pane',pane])[0],0)
        self.assertEqual(self.calls,[])
        for epoch in ('x'*257,'epoch\x00','epoch\x7f','\ud800'):
            self.epoch=epoch;self.assertEqual(self.run_cli(['--body'])[0],1);self.assertEqual(self.posts(),[])
    def test_exact_u64_metadata_decodes_without_rounding_and_not_in_body_or_output(self):
        snapshot=self.client.pane_read_json('GET','/api/cli/v1/snapshot');self.assertEqual(snapshot['revision'],(1<<64)-1)
        self.calls.clear();code,rows=self.run_cli(['--body']);self.assertEqual(code,0,rows);self.assertNotIn('revision',json.loads(self.posts()[0][3]));self.assertNotIn('revision',rows[0])
    def test_http_error_mapping_is_closed_by_status_code_and_exact_message(self):
        for status,value in ((409,{'code':'guard_mismatch','message':'SECRET'}),(400,{'code':'guard_mismatch','message':'guard target no longer matches'}),(502,{'code':'guarded_read_unavailable','message':'Guarded pane read is unavailable','data':'SECRET'}),(403,{'code':'SECRET','message':None})):
            self.calls.clear();self.failure=(status,{'error':value});code,rows=self.run_cli(['--body']);self.assertEqual((code,rows),(1,[{'error':{'code':'guarded_read_unavailable'}}]));self.assertEqual(len(self.posts()),1)
    def test_ctrl_c_real_main_body_socket_closes_without_retry(self):
        self.stall=('/pane/read','body')
        script="import sys;sys.path.insert(0,sys.argv[1]);import harness_herdr_cli as c;c.local.discover=lambda:c.local.Client('a'*64,'plugin',int(sys.argv[2]));sys.exit(c.main(['read','--device',sys.argv[3],'--pane','p','--body']))"
        child=subprocess.Popen([sys.executable,'-c',script,str(Path(cli.__file__).parent),str(self.server.server_address[1]),LOCAL],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env={**os.environ,'HOME':str(self.home)})
        self.addCleanup(lambda:(child.kill(),child.communicate()) if child.poll() is None else None)
        self.assertTrue(self.stalled.wait(3));child.send_signal(signal.SIGINT);out,err=child.communicate(timeout=3);self.assertEqual(child.returncode,1,err);self.assertTrue(self.closed.wait(1));self.assertEqual(len(self.posts()),1);self.assertNotIn('SECRET',out+err)

if __name__=='__main__':unittest.main()
