from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io, json, signal, sys, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))
import harness_herdr_cli as cli
import harness_herdr_local as local

DEVICE="11111111-1111-4111-8111-111111111111"
REMOTE="22222222-2222-4222-8222-222222222222"
TOKEN="t" * 64

def identity():
 return {"protocol":1,"device_id":DEVICE,"capabilities":{"read_metadata_v1":True,"guarded_mutation_v1":False},"native":{"version":None}}

def snapshot(device,status="working"):
 return {"protocol":1,"device_id":device,"server_epoch":"epoch","workspaces":[{"workspace_id":"ws"}],"tabs":[{"workspace_id":"ws","tab_id":"tab"}],"panes":[{"workspace_id":"ws","tab_id":"tab","pane_id":"pane","terminal_id":"term","agent":"custom-agent","agent_status":status}]}

class Loopback:
 def __init__(self):
  self.calls=[]; self.closed=threading.Event(); self.stall=None
  self.routes={"/api/cli/v1/identity":identity(),"/api/cli/v1/snapshot":snapshot(DEVICE),f"/api/machines/{REMOTE}/cli/v1/snapshot":snapshot(REMOTE)}
  outer=self
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):
    outer.calls.append((self.command,self.path,dict(self.headers)))
    if self.path==outer.stall:
     if outer.stall_mode=="body":
      self.send_response(200); self.send_header("Content-Length","100"); self.end_headers(); self.wfile.write(b"{"); self.wfile.flush()
     self.connection.settimeout(1)
     try:
      if self.connection.recv(1)==b"": outer.closed.set()
     except OSError: outer.closed.set()
     return
    encoded=json.dumps(outer.routes[self.path]).encode()
    self.send_response(200); self.send_header("Content-Length",str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
   def log_message(self,*args): pass
  self.httpd=ThreadingHTTPServer(("127.0.0.1",0),Handler); self.port=self.httpd.server_address[1]
  threading.Thread(target=self.httpd.serve_forever,daemon=True).start()
 def close(self): self.httpd.shutdown(); self.httpd.server_close()
class Client:
 def __init__(self): self.calls=[]
 def request_json(self,path,deadline_seconds=None):
  self.calls.append(path)
  return {"protocol":1,"device_id":DEVICE,"server_epoch":"epoch","workspaces":[{"workspace_id":"ws"}],"tabs":[{"workspace_id":"ws","tab_id":"tab"}],"panes":[{"workspace_id":"ws","tab_id":"tab","pane_id":"pane","terminal_id":"term","agent":"custom-agent","agent_status":"working"}]}
class TestAgentWait(unittest.TestCase):
 def test_observer_deadline_starts_before_identity_and_prevents_snapshot(self):
  clock=[0]
  class TimedClient:
   def __init__(self): self.calls=[]
   def request_json(self,path,deadline_seconds=None):
    self.calls.append((path,deadline_seconds)); clock[0]+=2
    if path=="/api/cli/v1/identity":
     return {"protocol":1,"device_id":DEVICE,"capabilities":{"read_metadata_v1":True,"guarded_mutation_v1":False},"native":{"version":None}}
    return {"protocol":1,"device_id":DEVICE,"server_epoch":"epoch","workspaces":[{"workspace_id":"ws"}],"tabs":[{"workspace_id":"ws","tab_id":"tab"}],"panes":[{"workspace_id":"ws","tab_id":"tab","pane_id":"pane","terminal_id":"term","agent":"agent","agent_status":"done"}]}
  client=TimedClient()
  with patch.object(cli.time,"monotonic",side_effect=lambda:clock[0]):
   with self.assertRaisesRegex(cli.CommandError,"wait_timeout"):
    cli.execute(["agent-wait","--device",DEVICE,"--pane","pane","--until","done","--timeout-seconds","1"],client)
  self.assertEqual([call[0] for call in client.calls],["/api/cli/v1/identity"])
  self.assertLessEqual(client.calls[0][1],1)
 def test_invalid_device_rejects_without_identity_request(self):
  client=Client()
  with self.assertRaisesRegex(cli.CommandError,"invalid_device_id"):
   cli.execute(["agent-wait","--device","not-a-device","--pane","pane","--until","done","--timeout-seconds","1"],client)
  self.assertEqual(client.calls,[])
 def test_malformed_wait_options_make_no_network_request(self):
  for argv in (("--until","bad","--timeout-seconds","1"),("--until","done","--until","done","--timeout-seconds","1"),("--until","done","--timeout-seconds","0")):
   with self.subTest(argv=argv):
    client=Client()
    with self.assertRaisesRegex(cli.CommandError,"invalid_selection"):
     cli.execute(["agent-wait","--device",DEVICE,"--pane","pane",*argv],client)
    self.assertEqual(client.calls,[])
 def test_main_rejects_very_long_timeout_without_network_or_traceback(self):
  client=Client(); out=io.StringIO()
  with patch.object(cli.local,"discover",return_value=client),patch("sys.stdout",out):
   self.assertEqual(cli.main(["agent-wait","--device",DEVICE,"--pane","pane","--until","done","--timeout-seconds","9" * 5000]),1)
  self.assertEqual(out.getvalue().strip(),'{"error": {"code": "invalid_selection"}}')
  self.assertEqual(client.calls,[])
 def test_fake_clock_limits_nonmatching_observer_to_240_sequential_snapshots(self):
  clock=[0]; client=Client()
  def request(path, deadline_seconds=None):
   client.calls.append((path,deadline_seconds)); return snapshot(DEVICE,"working")
  client.request_json=request
  with patch.object(cli.time,"monotonic",side_effect=lambda:clock[0]),patch.object(cli.time,"sleep",side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
   with self.assertRaisesRegex(cli.CommandError,"wait_timeout"):
    cli.agent_wait(client,DEVICE,DEVICE,"pane",["done"],240)
  self.assertEqual(len(client.calls),240)
  self.assertTrue(all(path=="/api/cli/v1/snapshot" and seconds>0 for path,seconds in client.calls))
 def test_initial_matching_status_is_safe_read_only_observation(self):
  client=Client(); out=io.StringIO()
  with patch.object(cli,"read_identity",return_value={"device_id":DEVICE}):
   value=cli.execute(["agent-wait","--device",DEVICE,"--pane","pane","--until","working","--timeout-seconds","1"],client)
  self.assertEqual(value,{"device_id":DEVICE,"server_epoch":"epoch","pane_id":"pane","terminal_id":"term","agent":"custom-agent","agent_status":"working","observation":"status_matched"})
 def test_rejects_changed_tuple_and_invalid_status_without_mutation(self):
  base={"protocol":1,"device_id":DEVICE,"server_epoch":"epoch","workspaces":[{"workspace_id":"ws"}],"tabs":[{"workspace_id":"ws","tab_id":"tab"}],"panes":[{"workspace_id":"ws","tab_id":"tab","pane_id":"pane","terminal_id":"term","agent":"agent","agent_status":"working"}]}
  for change,code in (({"server_epoch":"other"},"target_changed"),({"panes":[]},"target_changed"),({"panes":base["panes"]*2},"invalid_response"),({"panes":[{**base["panes"][0],"agent_status":"bad"}]},"invalid_response"),({"panes":[{**base["panes"][0],"agent":None}]},"target_changed")):
   with self.subTest(code=code):
    client=Client(); values=[base,{**base,**change}]; client.request_json=lambda path, deadline_seconds=None:values.pop(0)
    with self.assertRaisesRegex(cli.CommandError,code),patch.object(cli.time,"sleep",return_value=None): cli.agent_wait(client,DEVICE,DEVICE,"pane",["done"],cli.time.monotonic()+2)
 def test_null_agent_epoch_and_timeout_are_fixed(self):
  for value,code in (({"protocol":1,"device_id":DEVICE,"server_epoch":None,"workspaces":[],"tabs":[],"panes":[]},"epoch_unavailable"),):
   client=Client();client.request_json=lambda path, deadline_seconds=None:value
   with self.assertRaisesRegex(cli.CommandError,code):cli.agent_wait(client,DEVICE,DEVICE,"pane",["idle"],cli.time.monotonic()+1)
 def test_cancelled_main_prints_fixed_code(self):
  out=io.StringIO()
  with patch.object(cli.local,"discover",side_effect=KeyboardInterrupt),patch("sys.stdout",out):self.assertEqual(cli.main(["devices"]),1)
  self.assertEqual(out.getvalue().strip(),'{"error": {"code": "wait_cancelled"}}')
 def test_agent_wait_child_help_states_observation_limits(self):
  out=io.StringIO()
  with patch("sys.stdout",out),self.assertRaises(SystemExit): cli.main(["agent-wait","--help"])
  self.assertIn("tuple-stable sessions are undetectable",out.getvalue())
  self.assertIn("turn/process exit nor close authority",out.getvalue())

class TestAgentWaitTransport(unittest.TestCase):
 def setUp(self):
  self.server=Loopback(); self.addCleanup(self.server.close)
 def client(self): return local.Client(token=TOKEN,mode="managed",port=self.server.port)
 def test_main_uses_only_get_for_local_and_remote_matched_samples(self):
  for device,path in ((DEVICE,"/api/cli/v1/snapshot"),(REMOTE,f"/api/machines/{REMOTE}/cli/v1/snapshot")):
   with self.subTest(device=device):
    self.server.calls.clear(); out=io.StringIO()
    with patch.object(cli.local,"discover",return_value=self.client()),patch("sys.stdout",out):
     self.assertEqual(cli.main(["agent-wait","--device",device,"--pane","pane","--until","working","--timeout-seconds","1"]),0)
    self.assertEqual([call[1] for call in self.server.calls],["/api/cli/v1/identity",path])
    for method,_,headers in self.server.calls:
     self.assertEqual(method,"GET"); self.assertEqual(headers["Authorization"],f"Bearer {TOKEN}")
     self.assertNotIn("Content-Type",headers); self.assertNotIn("X-Herdr-Launcher",headers); self.assertNotIn("X-Herdr-Machine",headers)
    self.assertEqual(json.loads(out.getvalue())["observation"],"status_matched")
 def test_stalled_response_headers_and_body_close_socket_before_observer_deadline(self):
  for mode in ("headers","body"):
   with self.subTest(mode=mode):
    self.server.closed.clear(); self.server.stall="/api/cli/v1/snapshot"; self.server.stall_mode=mode
    started=time.monotonic()
    with self.assertRaisesRegex(cli.CommandError,"wait_timeout"):
     cli.agent_wait(self.client(),DEVICE,DEVICE,"pane",["done"],time.monotonic()+0.08)
    self.assertLess(time.monotonic()-started,0.5); self.assertTrue(self.server.closed.wait(0.5))
    self.server.stall=None
 def test_identity_stall_uses_observer_timeout_and_never_requests_snapshot(self):
  for mode in ("headers","body"):
   with self.subTest(mode=mode):
    self.server.calls.clear(); self.server.closed.clear(); self.server.stall="/api/cli/v1/identity"; self.server.stall_mode=mode; out=io.StringIO()
    with patch.object(cli.local,"discover",return_value=self.client()),patch("sys.stdout",out):
     self.assertEqual(cli.main(["agent-wait","--device",DEVICE,"--pane","pane","--until","done","--timeout-seconds","1"]),1)
    self.assertEqual(out.getvalue().strip(),'{"error": {"code": "wait_timeout"}}')
    self.assertEqual([path for _,path,_ in self.server.calls],["/api/cli/v1/identity"])
    self.assertTrue(self.server.closed.wait(0.5)); self.server.stall=None
 def test_os_interrupt_unwinds_stalled_socket_and_emits_safe_wait_cancellation(self):
  self.server.stall="/api/cli/v1/snapshot"; self.server.stall_mode="headers"; out=io.StringIO()
  previous=signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
  try:
   signal.setitimer(signal.ITIMER_REAL,0.08)
   with patch.object(cli.local,"discover",return_value=self.client()),patch("sys.stdout",out):
    self.assertEqual(cli.main(["agent-wait","--device",DEVICE,"--pane","pane","--until","done","--timeout-seconds","1"]),1)
  finally:
   signal.setitimer(signal.ITIMER_REAL,0); signal.signal(signal.SIGALRM,previous)
  self.assertEqual(out.getvalue().strip(),'{"error": {"code": "wait_cancelled"}}')
  self.assertTrue(self.server.closed.wait(0.5))
if __name__=="__main__": unittest.main()
