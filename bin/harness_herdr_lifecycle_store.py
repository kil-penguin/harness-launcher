"""Isolated private lifecycle receipts and closed untagged HTTP projections."""
import base64
import contextlib
import copy
import json
import os
from pathlib import Path
import secrets

import harness_herdr_store as private
import harness_herdr_local as local

U63=(1<<63)-1
U64=(1<<64)-1
METHODS={'lifecycle.guarded_'+name+'.v1' for name in ('create','interrupt','close')}
PHASES={'created','prepare_started','prepare_unknown','prepared','dispatch_started','terminal_observed'}
TERMINAL={'completed','cancelled','rejected'}
IDS={'workspace_id','tab_id','pane_id','terminal_id'}

class LifecycleStoreError(Exception):pass

def fail(code='lifecycle_receipt_invalid'):raise LifecycleStoreError(code)

def unsigned(value,maximum=U63):return type(value) is int and 0<=value<=maximum

def text(value,limit=256):
    try:return isinstance(value,str) and bool(value) and all(ord(c)>=32 and ord(c)!=127 for c in value) and len(value.encode('utf-8'))<=limit
    except UnicodeError:return False

def canonical_uuid(value):
    try:private._uuid(value)
    except private.StoreError:fail()
    return value

def secret(value,size=None):
    if not isinstance(value,str) or not value or len(value)>5462 or '=' in value:fail()
    try:raw=base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)
    except (ValueError,TypeError):fail()
    if base64.urlsafe_b64encode(raw).rstrip(b'=').decode()!=value or size is not None and len(raw)!=size or size is None and not 1<=len(raw)<=4096:fail()
    return value

def create_receipt(value):
    if not isinstance(value,dict) or set(value)!={'receipt_id','generation','resource_ids'} or not text(value['receipt_id']) or not unsigned(value['generation']):fail()
    ids=value['resource_ids']
    if not isinstance(ids,dict) or set(ids)!=IDS or any(not text(v) for v in ids.values()):fail()
    return copy.deepcopy(value)

def operation(value,record=None):
    required={'operation_id','server_epoch','method','state'}
    if not isinstance(value,dict) or not required<=value.keys():fail()
    canonical_uuid(value['operation_id'])
    if not text(value['server_epoch']) or not isinstance(value['method'],str) or value['method'] not in METHODS or not isinstance(value['state'],str):fail()
    if record is not None and (value['operation_id']!=record['operation_uuid'] or value['server_epoch']!=record['epoch'] or value['method']!=record['method']):fail('invalid_response')
    kind=value['method'].removeprefix('lifecycle.guarded_').removesuffix('.v1');state=value['state'];extras={}
    if state in {'prepared','queued','reserved'}:pass
    elif kind=='create' and state=='spawn_claimed':pass
    elif kind=='create' and state in {'spawned','installing','reconciling'}:extras={'lifecycle_effect':{'kind':'create','outcome':'spawned'}}
    elif state=='completed':
        outcomes={'create':{'resource_created'},'interrupt':{'signal_accepted','already_exited','signal_denied','unknown'},'close':{'resource_closed'}}
        effect=value.get('lifecycle_effect')
        if not isinstance(effect,dict) or set(effect)!={'kind','outcome'} or effect['kind']!=kind or not isinstance(effect['outcome'],str) or effect['outcome'] not in outcomes[kind]:fail()
        extras={'lifecycle_effect':effect}
        if kind=='create':extras['create_receipt']=create_receipt(value.get('create_receipt'))
    elif state=='cancelled':extras={'reason':'cancelled'}
    elif state=='rejected':
        reason=value.get('reason');allowed={'guard_mismatch','deadline_exceeded'}|({'close_precondition_failed'} if kind=='close' else set())
        if kind=='create' and reason=='spawn_failed':extras={'reason':'spawn_failed','lifecycle_effect':{'kind':'create','outcome':'spawn_failed'}}
        elif isinstance(reason,str) and reason in allowed:extras={'reason':reason}
        else:fail()
    else:fail()
    if set(value)!=required|extras.keys() or any(value[k]!=v for k,v in extras.items()):fail()
    return copy.deepcopy(value)

def validate(value,expected=None):
    required={'version','revision','intent','device_uuid','method','epoch','phase','owner_key'}
    if not isinstance(value,dict) or not required<=value.keys() or value.keys()-required-{'operation_uuid','ticket','create_receipt'}:fail()
    if type(value['version']) is not int or value['version']!=1 or not unsigned(value['revision']):fail()
    canonical_uuid(value['intent']);canonical_uuid(value['device_uuid']);secret(value['owner_key'],32)
    if expected is not None and value['intent']!=expected:fail()
    if not isinstance(value['method'],str) or value['method'] not in METHODS or not text(value['epoch']) or not isinstance(value['phase'],str) or value['phase'] not in PHASES:fail()
    has_op='operation_uuid' in value
    if has_op:canonical_uuid(value['operation_uuid']);secret(value.get('ticket'))
    elif 'ticket' in value or value['phase'] in {'prepared','dispatch_started','terminal_observed'}:fail()
    if value['phase'] in {'created','prepare_started','prepare_unknown'} and has_op:fail()
    if 'create_receipt' in value:
        create_receipt(value['create_receipt'])
        if value['method']=='lifecycle.guarded_create.v1' and value['phase']!='terminal_observed':fail()
    elif value['method']!='lifecycle.guarded_create.v1':fail()
    return value

class _Files(private.Store):
    def __init__(self,home):
        super().__init__(home);self.root=self.home/'.local/state/harness-herdr-web/lifecycle-receipts/v1'
    @contextlib.contextmanager
    def open_verified_root(self):
        fds=[]
        try:
            if not self.home.is_absolute() or any(p in {'.','..'} for p in self.home.parts):fail()
            fds.append(os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW))
            for part in self.home.parts[1:]:fds.append(os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fds[-1]))
            info=os.fstat(fds[-1])
            if info.st_uid!=self.uid or info.st_mode&0o022:fail()
            for part in ('.local','state'):
                try:fd=self._dir(part,fds[-1])
                except FileNotFoundError:fd=self._private_dir(fds[-1],part)
                fds.append(fd)
            for part in ('harness-herdr-web','lifecycle-receipts','v1'):fds.append(self._private_dir(fds[-1],part))
            yield fds[-1]
        finally:
            for fd in reversed(fds):os.close(fd)
    def _validate(self,value,expected=None):return validate(value,expected)
    def _read(self,root,intent):
        fd=os.open(self._record_name(intent),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
        try:
            info=os.fstat(fd)
            if not private._private_regular(info,self.uid) or info.st_size>private.MAX_RECORD_BYTES:fail()
            raw=b''
            while len(raw)<=private.MAX_RECORD_BYTES:
                piece=os.read(fd,min(8192,private.MAX_RECORD_BYTES+1-len(raw)))
                if not piece:break
                raw+=piece
        finally:os.close(fd)
        if len(raw)>private.MAX_RECORD_BYTES:fail()
        try:return validate(local.strict_json(raw.decode('utf-8'),canonical_unsigned=True),intent)
        except (UnicodeError,ValueError,TypeError,RecursionError):fail()
    def _write(self,root,value):
        validate(value);raw=json.dumps(value,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
        if len(raw)>private.MAX_RECORD_BYTES:fail()
        name='.lifecycle-'+secrets.token_hex(24)+'.tmp';fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
        try:
            if not private._private_regular(os.fstat(fd),self.uid):fail()
            data=memoryview(raw)
            while data:
                count=os.write(fd,data)
                if count<=0:raise OSError('write')
                data=data[count:]
            os.fsync(fd);os.close(fd);fd=None
            os.replace(name,self._record_name(value['intent']),src_dir_fd=root,dst_dir_fd=root);os.fsync(root)
        finally:
            if fd is not None:os.close(fd)
            try:os.unlink(name,dir_fd=root)
            except FileNotFoundError:pass

class LifecycleStore:
    def __init__(self,home=None):
        self._files=_Files(Path.home() if home is None else Path(home));self.root=self._files.root
    def create(self,intent,device,method,epoch,source=None):
        value={'version':1,'revision':0,'intent':intent,'device_uuid':device,'method':method,'epoch':epoch,'phase':'created','owner_key':base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode() if source is None else source['owner_key']}
        if source is not None:value['create_receipt']=create_receipt(source['create_receipt'])
        validate(value)
        with self._files._locked_root(intent) as root:
            try:os.stat(intent+'.json',dir_fd=root,follow_symlinks=False)
            except FileNotFoundError:self._files._write(root,value)
            else:fail('lifecycle_receipt_exists')
        return value
    def read(self,intent):
        canonical_uuid(intent)
        with self._files._locked_root(intent) as root:return self._files._read(root,intent)
    def transition(self,source,*,phase,prepared=None):
        validate(source)
        if source['revision']==U63:fail('lifecycle_receipt_changed')
        value=copy.deepcopy(source);value.update(revision=source['revision']+1,phase=phase)
        if prepared is not None:
            if 'operation_uuid' in source or source['phase']!='prepare_started' or phase!='prepared':fail('lifecycle_receipt_changed')
            value.update(operation_uuid=prepared['operation_id'],ticket=prepared['admission_ticket'])
        validate(value)
        with self._files._locked_root(source['intent']) as root:
            if self._files._read(root,source['intent'])!=source:fail('lifecycle_receipt_changed')
            self._files._write(root,value)
        return value

    def _marker_name(self,record,kind):return canonical_uuid(record['intent'])+'.'+kind
    def _marker_value(self,record):
        return {key:record[key] for key in ('intent','device_uuid','epoch','method','operation_uuid','owner_key','revision')}
    def _exists(self,root,record,kind):
        try:os.stat(self._marker_name(record,kind),dir_fd=root,follow_symlinks=False)
        except FileNotFoundError:return False
        return True
    def _read_marker(self,root,record,kind):
        name=self._marker_name(record,kind);fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
        try:
            info=os.fstat(fd)
            if not private._private_regular(info,self._files.uid) or info.st_size>private.MAX_RECORD_BYTES:fail()
            raw=b''
            while len(raw)<=private.MAX_RECORD_BYTES:
                piece=os.read(fd,min(8192,private.MAX_RECORD_BYTES+1-len(raw)))
                if not piece:break
                raw+=piece
            if len(raw)>private.MAX_RECORD_BYTES:fail()
            try:value=local.strict_json(raw.decode('utf-8'),canonical_unsigned=True)
            except (UnicodeError,ValueError,RecursionError):fail()
            expected=self._marker_value(record)
            if not isinstance(value,dict) or set(value)!=set(expected) or not unsigned(value.get('revision')) or value['revision']>record['revision'] or any(value[k]!=v for k,v in expected.items() if k!='revision'):fail('lifecycle_receipt_changed')
            return (value,raw,info.st_dev,info.st_ino)
        finally:os.close(fd)
    def _write_marker(self,root,record,kind):
        name=self._marker_name(record,kind);fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
        try:
            if not private._private_regular(os.fstat(fd),self._files.uid):fail()
            raw=memoryview(json.dumps(self._marker_value(record),separators=(',',':')).encode())
            while raw:
                count=os.write(fd,raw)
                if count<=0:raise OSError('write')
                raw=raw[count:]
            os.fsync(fd)
        finally:os.close(fd)
        os.fsync(root)
        return self._read_marker(root,record,kind)
    def _remove_marker(self,root,record,kind,token):
        if self._read_marker(root,record,kind)!=token:fail('lifecycle_receipt_changed')
        info=os.stat(self._marker_name(record,kind),dir_fd=root,follow_symlinks=False)
        if (info.st_dev,info.st_ino)!=(token[2],token[3]) or not private._private_regular(info,self._files.uid):fail('lifecycle_receipt_changed')
        # Unlink is the authority commit. No fallible post-unlink fsync; crash
        # resurrection conservatively requires the same-operation status again.
        os.unlink(self._marker_name(record,kind),dir_fd=root)
    def authorize_create(self,record):
        validate(record)
        with self._files._locked_root(record['intent']) as root:
            if self._files._read(root,record['intent'])!=record:fail('lifecycle_receipt_changed')
            if record['method']!='lifecycle.guarded_create.v1' or record['phase']!='terminal_observed' or 'create_receipt' not in record or self._exists(root,record,'recovery'):fail('lifecycle_receipt_unavailable')
            if self._exists(root,record,'get-only'):self._read_marker(root,record,'get-only')
        return record
    def cancel_allowed(self,record):
        with self._files._locked_root(record['intent']) as root:
            if self._files._read(root,record['intent'])!=record:fail('lifecycle_receipt_changed')
            if self._exists(root,record,'get-only'):fail('lifecycle_get_only')
    def reserve(self,source,kind):
        validate(source)
        if source['revision']==U63 or 'operation_uuid' not in source:fail('lifecycle_receipt_changed')
        value=copy.deepcopy(source);value['revision']+=1
        if kind=='dispatch':
            if source['phase']!='prepared':fail('lifecycle_get_only')
            value['phase']='dispatch_started'
        token=recovery=None
        with self._files._locked_root(source['intent']) as root:
            if self._files._read(root,source['intent'])!=source:fail('lifecycle_receipt_changed')
            exists=self._exists(root,source,'get-only')
            if exists and kind!='status':fail('lifecycle_get_only')
            if not exists:token=self._write_marker(root,value,'get-only')
            if kind=='dispatch' and source['method']=='lifecycle.guarded_create.v1':
                recovery=self._write_marker(root,value,'recovery')
            elif self._exists(root,source,'recovery'):
                recovery=self._read_marker(root,source,'recovery')
            self._files._write(root,value)
        # Pin original inode/content/tuple/revision before the network await.
        return value,{'get-only':token,'recovery':recovery}
    def observe(self,source,result,reservation):
        operation(result,source)
        if source['revision']==U63:fail('lifecycle_receipt_changed')
        value=copy.deepcopy(source);value['revision']+=1
        if result['state'] in TERMINAL:value['phase']='terminal_observed'
        completed=result['method']=='lifecycle.guarded_create.v1' and result['state']=='completed'
        if completed:
            receipt=create_receipt(result['create_receipt'])
            if 'create_receipt' in source and source['create_receipt']!=receipt:fail('invalid_response')
            value['create_receipt']=receipt
        validate(value)
        with self._files._locked_root(source['intent']) as root:
            if self._files._read(root,source['intent'])!=source:fail('lifecycle_receipt_changed')
            recovery=reservation['recovery'];get_only=reservation['get-only']
            if recovery is None:
                if self._exists(root,source,'recovery'):fail('lifecycle_receipt_changed')
            elif self._read_marker(root,source,'recovery')!=recovery:fail('lifecycle_receipt_changed')
            if completed and 'create_receipt' not in source and recovery is None:fail('lifecycle_receipt_unavailable')
            if get_only is not None and self._read_marker(root,source,'get-only')!=get_only:fail('lifecycle_receipt_changed')
            self._files._write(root,value)
            if get_only is not None:self._remove_marker(root,source,'get-only',get_only)
            if completed and recovery is not None:self._remove_marker(root,source,'recovery',recovery)
        return value
