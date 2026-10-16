"""Private no-replay receipts for Codex one-key UI requests."""
import copy
import contextlib
import json
import os
from pathlib import Path
import secrets
import time
import stat

import harness_herdr_store as private

MAX_U64=(1<<64)-1
STATES={"prepared","dispatch_started","awaiting_operation","completed","cancelled","stopped","unknown"}
PHASES={"intent_durable","prepare_started","prepared","dispatch_started","awaiting_operation","completed","cancelled","stopped","unknown","cancel_started"}
TERMINAL={"completed","cancelled","stopped","unknown"}
RETAIN_MS=10*60*1000
MAX_RECORDS=64

class ReceiptError(Exception): pass
def fail(code="receipt_invalid"): raise ReceiptError(code)
def uuid(value):
    try: private._uuid(value)
    except private.StoreError: fail()
    return value
def unsigned(value): return type(value) is int and 0<=value<=MAX_U64

def result(value, epoch=None, workflow_id=None):
    required={"workflow_id","server_epoch","state","revision"}
    if not isinstance(value,dict) or set(value)-required-{"current_operation"} or not required<=set(value): fail()
    uuid(value["workflow_id"])
    if not isinstance(value["server_epoch"],str) or not value["server_epoch"] or not isinstance(value["state"],str) or value["state"] not in STATES or not unsigned(value["revision"]): fail()
    if epoch is not None and value["server_epoch"]!=epoch or workflow_id is not None and value["workflow_id"]!=workflow_id: fail()
    if "current_operation" in value:
        op=value["current_operation"]
        if not isinstance(op,dict) or set(op)-{"operation_id","server_epoch","state","effect","reason"} or not {"operation_id","server_epoch","state","effect"}<=set(op): fail()
        uuid(op["operation_id"])
        effect=op["effect"]
        if op["server_epoch"]!=value["server_epoch"] or not isinstance(op["state"],str) or op["state"] not in {"prepared","queued","writing_text","waiting_enter","writing_enter","completed","cancelled","partial_unknown","rejected"} or not isinstance(effect,dict) or set(effect)!={"text_bytes_accepted","enter_accepted"} or not unsigned(effect["text_bytes_accepted"]) or type(effect["enter_accepted"]) is not bool: fail()
        if "reason" in op and (not isinstance(op["reason"],str) or op["reason"] not in {"guard_mismatch","deadline_exceeded","pty_closed","io_error","cancelled","partial_unknown"}): fail()
    return copy.deepcopy(value)

def validate(record, expected=None):
    keys={"version","intent","device_id","method","server_epoch","workflow_id","revision","phase","created_unix_ms"}
    if not isinstance(record,dict) or set(record)-keys-{"safe_result","terminal_unix_ms","_inode"} or not keys<=set(record): fail()
    if record["version"]!=1 or record["method"] not in {"interrupt","exit"} or record["phase"] not in PHASES or not unsigned(record["revision"]): fail()
    uuid(record["intent"]); uuid(record["device_id"])
    if expected is not None and expected!=record["intent"]: fail()
    if not isinstance(record["server_epoch"],str) or not record["server_epoch"]: fail()
    if not unsigned(record["created_unix_ms"]): fail()
    if "terminal_unix_ms" in record and (not unsigned(record["terminal_unix_ms"]) or record["terminal_unix_ms"]<record["created_unix_ms"] or record["phase"] not in TERMINAL): fail()
    if record["workflow_id"] is not None: uuid(record["workflow_id"])
    if "safe_result" in record:
        if record["workflow_id"] is None: fail()
        result(record["safe_result"],record["server_epoch"],record["workflow_id"])
    if "_inode" in record and (type(record["_inode"]) is not int or record["_inode"]<=0): fail()
    return record

class _Files(private.Store):
    def __init__(self,home): super().__init__(home); self.root=self.home/".local/state/harness-herdr-web/agent-ui-request-receipts/v1"
    @contextlib.contextmanager
    def open_verified_root(self):
        fds=[]
        try:
            if not self.home.is_absolute(): fail("receipt_unsafe")
            fds.append(os.open("/",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW))
            for part in self.home.parts[1:]: fds.append(os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fds[-1]))
            if os.fstat(fds[-1]).st_uid!=self.uid or os.fstat(fds[-1]).st_mode&0o022: fail("receipt_unsafe")
            for part in (".local","state","harness-herdr-web","agent-ui-request-receipts","v1"): fds.append(self._private_dir(fds[-1],part))
            yield fds[-1]
        finally:
            for fd in reversed(fds): os.close(fd)
    def _read(self,root,intent):
        try:
            value=super()._read(root,intent)
            value["_inode"]=os.stat(self._record_name(intent),dir_fd=root,follow_symlinks=False).st_ino
            return value
        except (UnicodeError,ValueError,TypeError,RecursionError): fail()
    def _validate(self,record,expected=None): return validate(record,expected)
    def _write(self,root,record):
        validate(record); persisted=copy.deepcopy(record); persisted.pop("_inode",None); raw=json.dumps(persisted,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
        if len(raw)>private.MAX_RECORD_BYTES: fail()
        tmp=".ui-request-"+secrets.token_hex(24)+".tmp"; fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
        try:
            if not private._private_regular(os.fstat(fd),self.uid): fail()
            view=memoryview(raw)
            while view:
                count=os.write(fd,view)
                if count<=0: raise OSError("write")
                view=view[count:]
            os.fsync(fd); os.close(fd); fd=None; os.replace(tmp,self._record_name(record["intent"]),src_dir_fd=root,dst_dir_fd=root); os.fsync(root)
        finally:
            if fd is not None: os.close(fd)
            try: os.unlink(tmp,dir_fd=root)
            except FileNotFoundError: pass

class Store:
    def __init__(self,home=None): self._files=_Files(Path.home() if home is None else Path(home)); self.root=self._files.root
    def create(self,intent,device,method,epoch):
        record=validate({"version":1,"intent":intent,"device_id":device,"method":method,"server_epoch":epoch,"workflow_id":None,"revision":0,"phase":"intent_durable","created_unix_ms":int(time.time()*1000)})
        with self._files._locked_root(intent) as root:
            with self._capacity_lock(root):
                self._prune(root,record["created_unix_ms"])
                try: os.stat(intent+".json",dir_fd=root,follow_symlinks=False)
                except FileNotFoundError: self._files._write(root,record)
                else: fail("receipt_exists")
                record=self._files._read(root,intent)
        return record
    def _prune(self,root,now):
        values=[]
        for name in os.listdir(root):
            if name.endswith(".json"):
                intent=name[:-5]; uuid(intent); value=self._files._read(root,intent)
                stamp=value.get("terminal_unix_ms",value["created_unix_ms"])
                values.append((stamp,intent))
        expired=[intent for stamp,intent in values if now-stamp>RETAIN_MS]
        retained=[item for item in values if item[1] not in expired]
        if len(retained)>=MAX_RECORDS: fail("receipt_capacity")
        for intent in expired: os.unlink(intent+".json",dir_fd=root)
        if expired: os.fsync(root)
    @contextlib.contextmanager
    def _capacity_lock(self,root):
        fd=os.open(".capacity.lock",os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=root)
        try:
            info=os.fstat(fd)
            if not private._private_regular(info,self._files.uid): fail()
            private.fcntl.flock(fd,private.fcntl.LOCK_EX)
            yield
        finally: os.close(fd)
    def read(self,intent):
        uuid(intent)
        with self._files._locked_root(intent) as root: return self._files._read(root,intent)
    def transition(self,source,phase,workflow_id=None,safe_result=None):
        validate(source)
        if source["revision"]==MAX_U64: fail("receipt_changed")
        value=copy.deepcopy(source); value["revision"]+=1; value["phase"]=phase
        if phase in TERMINAL:
            if "terminal_unix_ms" not in value: value["terminal_unix_ms"]=int(time.time()*1000)
        else: value.pop("terminal_unix_ms",None)
        if workflow_id is not None:
            if source["workflow_id"] not in {None,workflow_id}: fail("receipt_changed")
            value["workflow_id"]=workflow_id
        if safe_result is not None: value["safe_result"]=result(safe_result,source["server_epoch"],value["workflow_id"])
        validate(value)
        with self._files._locked_root(source["intent"]) as root:
            if self._files._read(root,source["intent"])!=source: fail("receipt_changed")
            self._files._write(root,value)
            value=self._files._read(root,source["intent"])
        return value
