import json, multiprocessing, os, sys, tempfile, unittest, uuid
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / 'bin'))
import harness_herdr_store as store

def lock_attempt(home, intent, queue):
 s=store.Store(home=home)
 try:
  with s.lock(intent): queue.put('acquired')
 except store.StoreBusy: queue.put('busy')

class StoreTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.home=Path(self.tmp.name)/'home'; (self.home/'.local/state').mkdir(parents=True)
  for p in (self.home,self.home/'.local',self.home/'.local/state'): p.chmod(0o700)
  self.s=store.Store(home=self.home); self.i=str(uuid.uuid4()); self.d=str(uuid.uuid4())
 def test_persist_read_cas_and_prepare_unknown(self):
  r=self.s.create_intent(self.i,self.d,'lifecycle.guarded_create.v1',create=True)
  self.assertEqual(r['state'],'intent_durable'); self.assertEqual(len(r['owner_key']),43)
  a=self.s.transition(self.i,0,'prepare_started'); self.assertEqual(a['revision'],1)
  u=self.s.transition(self.i,1,'prepare_unknown'); self.assertTrue(self.s.reconcile_only(self.i)); self.assertEqual(u['state'],'prepare_unknown')
  with self.assertRaises(store.StoreError): self.s.transition(self.i,1,'prepared_durable')
 def test_rejects_unsafe_and_strict_json(self):
  root=self.s.root; root.mkdir(parents=True); root.chmod(0o700)
  p=root/f'{self.i}.json'; p.write_text('{"version":1,"version":1}') ; p.chmod(0o600)
  with self.assertRaises(store.StoreError): self.s.read(self.i)
  p.unlink(); os.mkfifo(p)
  with self.assertRaises(store.StoreError) as e: self.s.read(self.i)
  self.assertNotIn('owner_key',str(e.exception))
 def test_lock_is_nonblocking(self):
  self.s.create_intent(self.i,self.d,'lifecycle.guarded_create.v1',create=True)
  with self.s.lock(self.i):
   with self.assertRaises(store.StoreBusy): self.s.lock(self.i).__enter__()
 def test_read_rejects_record_whose_uuid_differs_from_filename(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  other=str(uuid.uuid4())
  record={'version':1,'revision':0,'local_intent_uuid':other,'device_uuid':self.d,
          'method':'pane.guarded_input.v1','state':'intent_durable','safe_identity':{}}
  path=self.s.root/f'{self.i}.json'; path.write_text(json.dumps(record)); path.chmod(0o600)
  with self.assertRaises(store.StoreError): self.s.read(self.i)
 def test_bind_once_cas_and_state_write_ahead(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  started=self.s.transition(self.i,0,'prepare_started')
  op=str(uuid.uuid4())
  prepared=self.s.bind_operation(self.i,started['revision'],'epoch',op,'ticket')
  self.assertEqual(prepared['state'],'prepared_durable')
  self.assertEqual(prepared['operation']['operation_uuid'],op)
  with self.assertRaises(store.StoreError): self.s.bind_operation(self.i,started['revision'],'epoch',op,'ticket')
  dispatched=self.s.transition(self.i,prepared['revision'],'dispatch_started')
  self.assertEqual(dispatched['state'],'dispatch_started')
  terminal=self.s.observe_terminal(self.i,dispatched['revision'])
  self.assertEqual(terminal['state'],'observed_terminal')
 def test_invalid_uuid_does_not_create_store_paths(self):
  with self.assertRaises(store.StoreError): self.s.create_intent('../bad',self.d,'pane.guarded_input.v1')
  self.assertFalse(self.s.root.exists())
 def test_rejects_unsafe_leaf_symlink_mode_hardlink_and_fifo(self):
  parent=self.home/'.local/state'; leaf=parent/'harness-herdr-web'
  os.symlink(self.home,leaf)
  with self.assertRaises(store.StoreError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  leaf.unlink(); leaf.mkdir(mode=0o755)
  with self.assertRaises(store.StoreError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  leaf.chmod(0o700); (leaf/'v1').mkdir(mode=0o700)
  path=leaf/'v1'/f'{self.i}.json'; path.write_text('{}'); path.chmod(0o600)
  os.link(path,leaf/'v1'/'linked')
  with self.assertRaises(store.StoreError): self.s.read(self.i)
  path.unlink(); os.mkfifo(path)
  with self.assertRaises(store.StoreError): self.s.read(self.i)
 def test_rejects_nested_unknown_bool_nul_and_prepared_without_operation(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  path=self.s.root/f'{self.i}.json'
  cases=[
   {'version':1,'revision':True,'local_intent_uuid':self.i,'device_uuid':self.d,'method':'pane.guarded_input.v1','state':'intent_durable','safe_identity':{}},
   {'version':1,'revision':0,'local_intent_uuid':self.i,'device_uuid':self.d,'method':'pane.guarded_input.v1','state':'prepared_durable','safe_identity':{}},
   {'version':1,'revision':0,'local_intent_uuid':self.i,'device_uuid':self.d,'method':'pane.guarded_input.v1','state':'intent_durable','safe_identity':{'session':'x'}},
  ]
  for value in cases:
   path.write_text(json.dumps(value)); path.chmod(0o600)
   with self.assertRaises(store.StoreError): self.s.read(self.i)
  path.write_bytes(b'{"version":1}\0'); path.chmod(0o600)
  with self.assertRaises(store.StoreError): self.s.read(self.i)
 def test_prepare_started_is_reconcile_only_until_operation_is_bound(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  started=self.s.transition(self.i,0,'prepare_started')
  self.assertTrue(self.s.reconcile_only(self.i))
  with self.assertRaises(store.StoreError): self.s.transition(self.i,started['revision'],'dispatch_started')
  with self.assertRaises(store.StoreError): self.s.observe_terminal(self.i,started['revision'])
 def test_real_second_process_cannot_take_lock(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  queue=multiprocessing.Queue()
  with self.s.lock(self.i):
   child=multiprocessing.Process(target=lock_attempt,args=(str(self.home),self.i,queue))
   child.start(); child.join(10)
  self.assertEqual(child.exitcode,0); self.assertEqual(queue.get(timeout=2),'busy')
 def test_zero_write_and_fsync_failure_never_report_success(self):
  old_write=store.os.write
  store.os.write=lambda fd,data: 0
  try:
   with self.assertRaises(OSError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  finally: store.os.write=old_write
  self.assertFalse((self.s.root/f'{self.i}.json').exists())
  old_fsync=store.os.fsync
  store.os.fsync=lambda fd: (_ for _ in ()).throw(OSError('fsync'))
  try:
   with self.assertRaises(OSError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  finally: store.os.fsync=old_fsync
  self.assertFalse((self.s.root/f'{self.i}.json').exists())
 def test_short_write_completes_full_record(self):
  old_write=store.os.write
  store.os.write=lambda fd,data: old_write(fd,data[:1])
  try:
   self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  finally: store.os.write=old_write
  self.assertEqual(self.s.read(self.i)['local_intent_uuid'],self.i)
 def test_record_symlink_wrong_mode_and_duplicate_key_are_rejected(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  path=self.s.root/f'{self.i}.json'; saved=self.s.root/'saved'
  path.rename(saved); os.symlink(saved,path)
  with self.assertRaises(store.StoreError): self.s.read(self.i)
  path.unlink(); saved.rename(path); path.chmod(0o644)
  with self.assertRaises(store.StoreError): self.s.read(self.i)
  path.chmod(0o600); path.write_text('{"version":1,"version":1}'); path.chmod(0o600)
  with self.assertRaises(store.StoreError): self.s.read(self.i)
 def test_record_with_foreign_owner_is_rejected(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  real_fstat=store.os.fstat
  def foreign_regular(fd):
   value=real_fstat(fd)
   if os.path.stat.S_ISREG(value.st_mode):
    raw=list(value); raw[4]=value.st_uid+1
    return os.stat_result(raw)
   return value
  with patch.object(store.os,'fstat',foreign_regular):
   with self.assertRaises(store.StoreError): self.s.read(self.i)
 def test_rename_and_directory_fsync_failure_do_not_succeed(self):
  old_replace=store.os.replace
  store.os.replace=lambda *args,**kwargs: (_ for _ in ()).throw(OSError('rename'))
  try:
   with self.assertRaises(OSError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  finally: store.os.replace=old_replace
  self.assertFalse((self.s.root/f'{self.i}.json').exists())
  calls=[]; old_fsync=store.os.fsync
  def fail_directory(fd):
   calls.append(fd)
   if len(calls)==2: raise OSError('directory fsync')
   return old_fsync(fd)
  store.os.fsync=fail_directory
  try:
   with self.assertRaises(OSError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  finally: store.os.fsync=old_fsync
 def test_open_root_fd_cannot_be_redirected_by_path_replacement(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  other=str(uuid.uuid4())
  record={'version':1,'revision':0,'local_intent_uuid':other,'device_uuid':self.d,
          'method':'pane.guarded_input.v1','state':'intent_durable','safe_identity':{}}
  displaced=self.s.root.parent/'displaced'
  with self.s.open_verified_root() as fd:
   os.rename(self.s.root,displaced)
   self.s.root.mkdir(mode=0o700)
   self.s._write(fd,record)
  self.assertTrue((displaced/f'{other}.json').exists())
  self.assertFalse((self.s.root/f'{other}.json').exists())
 def test_new_private_leaf_entries_are_fsynced_before_record_write(self):
  events=[]; real_write=store.os.write; real_fsync=store.os.fsync
  store.os.write=lambda fd,data: (events.append('write'),real_write(fd,data))[1]
  store.os.fsync=lambda fd: (events.append('fsync'),real_fsync(fd))[1]
  try:
   self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  finally:
   store.os.write=real_write; store.os.fsync=real_fsync
  self.assertEqual(events[0],'fsync')
  self.assertGreaterEqual(events[:3].count('fsync'),2)
 def test_new_leaf_parent_fsync_failure_blocks_write_and_next_open_recovers(self):
  real_fsync=store.os.fsync; calls=[]
  def fail_first_parent(fd):
   calls.append(fd)
   if len(calls)==1: raise OSError('parent fsync')
   return real_fsync(fd)
  store.os.fsync=fail_first_parent
  try:
   with self.assertRaises(OSError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  finally: store.os.fsync=real_fsync
  self.assertFalse((self.s.root/f'{self.i}.json').exists())
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  self.assertEqual(self.s.read(self.i)['revision'],0)
 def test_existing_private_leaf_requires_exact_0700_and_is_not_repaired(self):
  leaf=self.home/'.local/state/harness-herdr-web'; leaf.mkdir(mode=0o700)
  (leaf/'v1').mkdir(mode=0o500)
  with self.assertRaises(store.StoreError): self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  self.assertEqual((leaf/'v1').stat().st_mode & 0o777,0o500)
 def test_version_must_be_an_integer_not_bool_or_float(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  path=self.s.root/f'{self.i}.json'
  for version in (True,1.0):
   path.write_text(json.dumps({'version':version,'revision':0,'local_intent_uuid':self.i,
    'device_uuid':self.d,'method':'pane.guarded_input.v1','state':'intent_durable','safe_identity':{}}))
   path.chmod(0o600)
   with self.assertRaises(store.StoreError): self.s.read(self.i)
 def test_unknown_operation_can_record_terminal_create_receipt(self):
  self.s.create_intent(self.i,self.d,'lifecycle.guarded_create.v1',create=True)
  started=self.s.transition(self.i,0,'prepare_started')
  prepared=self.s.bind_operation(self.i,started['revision'],'epoch',str(uuid.uuid4()),'ticket')
  unknown=self.s.observe_terminal(self.i,prepared['revision'],unknown=True)
  receipt={'receipt_id':'receipt','generation':1,'resource_ids':{
   'workspace_id':'w','tab_id':'t','pane_id':'p','terminal_id':'x'}}
  observed=self.s.observe_terminal(self.i,unknown['revision'],create_receipt=receipt)
  self.assertEqual(observed['state'],'observed_terminal')
  self.assertEqual(observed['create_receipt'],receipt)
 def test_wrong_method_state_and_lone_surrogate_fail_closed_without_secret(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  path=self.s.root/f'{self.i}.json'
  base={'version':1,'revision':0,'local_intent_uuid':self.i,'device_uuid':self.d,
        'method':'pane.guarded_input.v1','state':'intent_durable','safe_identity':{}}
  for field,value in [('method',[]),('state',{}),('safe_identity',{'pane_id':'\ud800'})]:
   record=dict(base); record[field]=value
   path.write_bytes(json.dumps(record,ensure_ascii=True).encode()); path.chmod(0o600)
   with self.assertRaises(store.StoreError) as error: self.s.read(self.i)
   self.assertEqual(str(error.exception),'private_store_invalid')
   self.assertNotIn('secret',str(error.exception))
 def test_bind_and_terminal_cas_reject_boolean_revisions(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  started=self.s.transition(self.i,0,'prepare_started')
  with self.assertRaises(store.StoreError): self.s.bind_operation(self.i,True,'epoch',str(uuid.uuid4()),'ticket')
  prepared=self.s.bind_operation(self.i,started['revision'],'epoch',str(uuid.uuid4()),'ticket')
  with self.assertRaises(store.StoreError): self.s.observe_terminal(self.i,True)
  self.assertEqual(self.s.read(self.i)['revision'],prepared['revision'])
 def test_file_and_target_directory_fsync_failures_return_no_success(self):
  self.s.create_intent(self.i,self.d,'pane.guarded_input.v1')
  root_inode=self.s.root.stat().st_ino; real_fsync=store.os.fsync
  def fail_file(fd):
   if os.fstat(fd).st_ino != root_inode and os.path.stat.S_ISREG(os.fstat(fd).st_mode):
    raise OSError('file fsync')
   return real_fsync(fd)
  store.os.fsync=fail_file
  try:
   with self.assertRaises(OSError): self.s.transition(self.i,0,'prepare_started')
  finally: store.os.fsync=real_fsync
  self.assertEqual(self.s.read(self.i)['revision'],0)
  def fail_target_directory(fd):
   if os.fstat(fd).st_ino == root_inode: raise OSError('target directory fsync')
   return real_fsync(fd)
  store.os.fsync=fail_target_directory
  try:
   with self.assertRaises(OSError): self.s.transition(self.i,0,'prepare_started')
  finally: store.os.fsync=real_fsync
if __name__=='__main__': unittest.main()
