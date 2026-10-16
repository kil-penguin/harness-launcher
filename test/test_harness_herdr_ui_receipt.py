"""Private UI receipt invariants and crash-point recovery, independent of HTTP."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'bin'))
import harness_herdr_ui_receipt as receipt
import harness_herdr_store as guarded_store

INTENT='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
DEVICE='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
WORKFLOW='cccccccc-cccc-4ccc-8ccc-cccccccccccc'

def result(**changes):
    return {'workflow_id':WORKFLOW,'server_epoch':'epoch','state':'ready','next_step':0,'step_count':2,'revision':0,**changes}

class UIReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp'); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve(); self.home.chmod(0o700)
        self.db=receipt.UIReceiptStore(self.home)
        self.record=self.db.create(INTENT,DEVICE,'epoch')

    def known(self):
        self.record=self.db.transition(self.record,phase='known',workflow_id=WORKFLOW,last_result=result())
        return self.record

    def test_original_generation_and_immutable_tuple_cas(self):
        original=self.known()
        changed=self.db.transition(original,phase='uncertain')
        with self.assertRaisesRegex(receipt.ReceiptError,'receipt_changed'):
            self.db.transition(original,phase='known',last_result=result())
        self.assertEqual(self.db.read(INTENT),changed)
        original=copy.deepcopy(changed); original['device_id']=WORKFLOW
        with self.assertRaisesRegex(receipt.ReceiptError,'receipt_changed'):
            self.db.transition(original,phase='known',last_result=result())
        self.assertEqual(self.db.read(INTENT)['device_id'],DEVICE)

    def test_u64_generation_overflow_refuses_before_replacement(self):
        record=self.known(); record['local_generation']=(1<<64)-1
        path=self.db.root/(INTENT+'.json'); path.write_text(json.dumps(record))
        with self.assertRaisesRegex(receipt.ReceiptError,'receipt_changed'):
            self.db.transition(record,phase='step_started',attempt={'step':0,'revision':0})
        self.assertEqual(self.db.read(INTENT),record)

    def test_closed_schema_rejects_secret_extra_duplicate_and_unsafe_file(self):
        path=self.db.root/(INTENT+'.json'); original=path.read_bytes()
        for raw in (b'{"version":1,"version":1}',json.dumps({**self.record,'answer':'SECRET'}).encode(),b'X'*65537):
            path.write_bytes(raw)
            with self.assertRaises((receipt.ReceiptError,guarded_store.StoreError)): self.db.read(INTENT)
        path.write_bytes(original); path.chmod(0o644)
        with self.assertRaises(guarded_store.StoreError): self.db.read(INTENT)
        path.chmod(0o600); linked=self.db.root/'link'; os.link(path,linked)
        with self.assertRaises(guarded_store.StoreError): self.db.read(INTENT)
        linked.unlink(); path.unlink(); path.symlink_to(self.home/'absent')
        with self.assertRaises(guarded_store.StoreError): self.db.read(INTENT)

    def test_record_parent_symlink_is_never_followed(self):
        other=self.home/'other'; other.mkdir(mode=0o700)
        root=self.db.root; moved=root.with_name('moved'); root.rename(moved); root.symlink_to(other,target_is_directory=True)
        with self.assertRaises((receipt.ReceiptError,guarded_store.StoreError)): self.db.read(INTENT)
        self.assertEqual(list(other.iterdir()),[])

    def test_full_u64_and_native_nine_state_projection_without_fake_effects(self):
        for state in ('prepared','queued','writing_text','waiting_enter','writing_enter','completed','cancelled','partial_unknown','rejected'):
            effect={'text_bytes_accepted':0,'enter_accepted':False} if state in {'prepared','queued','cancelled','rejected'} else {'text_bytes_accepted':(1<<64)-1,'enter_accepted':True}
            value=result(revision=(1<<64)-1,current_operation={'operation_id':WORKFLOW,'server_epoch':'epoch','state':state,'effect':effect})
            self.assertEqual(receipt.workflow(value),value)
        for operation in ({'operation_id':WORKFLOW,'server_epoch':'epoch','state':'completed'}, {'operation_id':WORKFLOW,'server_epoch':'epoch','state':'completed','effect':{'text_bytes_accepted':1<<64,'enter_accepted':False}}, {'operation_id':WORKFLOW,'server_epoch':'epoch','state':'completed','effect':{'text_bytes_accepted':0,'enter_accepted':False},'reason':'SECRET'}):
            with self.assertRaises(receipt.ReceiptError): receipt.workflow(result(current_operation=operation))

    def test_malformed_deep_or_huge_numeric_record_has_fixed_receipt_error(self):
        path=self.db.root/(INTENT+'.json')
        for raw in (b'{"x":'+b'['*1000+b'0'+b']'*1000+b'}',b'{"local_generation":'+b'9'*5000+b'}'):
            path.write_bytes(raw)
            try: self.db.read(INTENT)
            except (receipt.ReceiptError,guarded_store.StoreError): pass
            except Exception as error: self.fail('raw parser exception escaped: '+type(error).__name__)
            else: self.fail('malformed record accepted')

    def test_actual_web_completed_projection_uses_last_step_index(self):
        value=result(state='completed_ui_sequence',next_step=1,step_count=2,revision=2)
        self.assertEqual(receipt.workflow(value),value)

    def test_write_ahead_marker_survives_published_result_parent_fsync_failure(self):
        self.known()
        reserved=self.db.transition(self.record,phase='step_started',attempt={'step':0,'revision':0})
        marker=self.db.root/(INTENT+'.step-ban')
        self.assertTrue(marker.exists(), 'step reservation requires durable no-replay marker')
        original=os.fsync
        def failed(fd):
            # Fail only after a ready result has been atomically published.
            path=self.db.root/(INTENT+'.json')
            if json.loads(path.read_text())['phase']=='known': raise OSError('SECRET')
            return original(fd)
        with patch.object(os,'fsync',side_effect=failed):
            with self.assertRaises(OSError): self.db.transition(reserved,phase='known',last_result=result(next_step=1,revision=2))
        self.assertTrue(marker.exists())
        current=self.db.read(INTENT)
        # Simulate a later successful GET ready persistence, then explicit step.
        current=self.db.transition(current,phase=current['phase'],last_result=result(next_step=1,revision=2),attempt=current['attempt'])
        with self.assertRaisesRegex(receipt.ReceiptError,'reconcile_only'):
            self.db.transition(current,phase='step_started',attempt={'step':1,'revision':2})

if __name__=='__main__': unittest.main()
