"""Anchored private receipts for recoverable managed workspace launches."""
import json, os, re, stat
from pathlib import Path

PHASES={"allocating","preparing","starting","started","failed","unknown"}
UUID=re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
PARTS=(".local","state","harness-herdr-web","launch-receipts","v1")
class ReceiptError(Exception): pass

class LaunchReceiptStore:
 def __init__(self,home=None): self.home=Path.home() if home is None else Path(home); self.root=self.home.joinpath(*PARTS)
 def path(self,intent): return self.root/(self._uuid(intent)+".json")
 def create(self,intent,device_id,request_id):
  record=self._record(intent,device_id,request_id,"unknown")
  with self._root(True) as root:
   name=self._name(intent)
   try: fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
   except FileExistsError: raise ReceiptError("receipt_exists") from None
   try: self._write(fd,record); os.fsync(fd); os.fsync(root)
   except BaseException:
    os.close(fd)
    try: os.unlink(name,dir_fd=root)
    except OSError: pass
    raise
   os.close(fd)
  return self._public(record)
 def read(self,intent):
  with self._root() as root: return self._read(root,intent)
 def transition(self,record,phase):
  if not isinstance(record,dict) or not isinstance(phase,str) or phase not in PHASES: raise ReceiptError("receipt_invalid")
  next_record=dict(record); next_record["phase"]=phase
  with self._root() as root:
   current=self._read(root,record.get("intent"))
   if current != record: raise ReceiptError("receipt_changed")
   self._replace(root,record["intent"],next_record)
  return next_record
 def update(self,intent,phase,job=None): return self._public(self.transition(self.read(intent),phase))
 def _read(self,root,intent):
  try: fd=os.open(self._name(intent),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
  except OSError: raise ReceiptError("receipt_invalid") from None
  try:
   info=os.fstat(fd)
   if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>4096: raise ReceiptError("receipt_invalid")
   value=json.loads(os.read(fd,4097).decode(),object_pairs_hook=self._pairs)
  except (OSError,UnicodeError,ValueError,TypeError): raise ReceiptError("receipt_invalid") from None
  finally: os.close(fd)
  if value != self._record(intent,value.get("device_id") if isinstance(value,dict) else None,value.get("request_id") if isinstance(value,dict) else None,value.get("phase") if isinstance(value,dict) else None): raise ReceiptError("receipt_invalid")
  return value
 def _replace(self,root,intent,record):
  name=self._name(intent); temp="."+name+".pending"
  try:
   fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
   try: self._write(fd,record); os.fsync(fd)
   finally: os.close(fd)
   os.replace(temp,name,src_dir_fd=root,dst_dir_fd=root); os.fsync(root)
  finally:
   try: os.unlink(temp,dir_fd=root)
   except OSError: pass
 def _root(self,create=False): return _Root(self.home,create)
 def _record(self,intent,device,request,phase):
  if not self._uuid(intent) or not self._uuid(device) or not self._uuid(request) or not isinstance(phase,str) or phase not in PHASES: raise ReceiptError("receipt_invalid")
  return {"version":1,"intent":intent,"device_id":device,"request_id":request,"phase":phase}
 def _name(self,intent): return self._uuid(intent)+".json"
 def _uuid(self,value):
  if not isinstance(value,str) or UUID.fullmatch(value) is None: raise ReceiptError("receipt_invalid")
  return value
 def _write(self,fd,record):
  data=json.dumps(record,separators=(",",":"),ensure_ascii=True).encode()
  while data:
   count=os.write(fd,data)
   if count<=0: raise OSError("write")
   data=data[count:]
 def _pairs(self,items):
  result={}
  for key,value in items:
   if key in result: raise ValueError("duplicate")
   result[key]=value
  return result
 def _public(self,record): return {key:record[key] for key in ("intent","device_id","request_id","phase")}

class _Root:
 def __init__(self,home,create): self.home,self.create,self.fd=home,create,None
 def __enter__(self):
  try: fd=os.open(self.home,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  except OSError: raise ReceiptError("receipt_unsafe") from None
  try:
   for part in PARTS:
    try: child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    except FileNotFoundError:
     if not self.create: raise ReceiptError("receipt_unsafe") from None
     os.mkdir(part,0o700,dir_fd=fd); os.fsync(fd); child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    except OSError: raise ReceiptError("receipt_unsafe") from None
    info=os.fstat(child)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700: os.close(child); raise ReceiptError("receipt_unsafe")
    os.close(fd); fd=child
   self.fd=fd; return fd
  except BaseException: os.close(fd); raise
 def __exit__(self,*_): os.close(self.fd)
