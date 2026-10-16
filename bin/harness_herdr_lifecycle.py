"""One explicit owned lifecycle operation; recovery never dispatches an action."""
import json
import uuid
import harness_herdr_files as files
import harness_herdr_lifecycle_store as store

class LifecycleError(Exception):pass

def fail(code='lifecycle_invalid_request'):raise LifecycleError(code)

def emit(stream,value):
    stream.write(json.dumps(value,separators=(',',':'),allow_nan=False)+'\n');stream.flush()

def safe(record,operation=None):
    value={'intent':record['intent'],'device_id':record['device_uuid'],'method':record['method']}
    if operation is not None:value['operation']=operation
    return value

def prefix(device,local_id):return '/api/cli/v1' if device==local_id else f'/api/machines/{device}/cli/v1'

class _Client:
    def __init__(self,client):self.client=client
    def request_json(self,path):return self.client.lifecycle_json('GET',path)
    def post_json(self,path,body):return self.client.lifecycle_json('POST',path,body)

def checked_prepare(value,record):
    try:
        if not isinstance(value,dict) or set(value)!={'operation_id','server_epoch','admission_ticket','deadline_monotonic_ms'}:fail('invalid_response')
        store.canonical_uuid(value['operation_id']);store.secret(value['admission_ticket'])
        if value['server_epoch']!=record['epoch'] or not store.unsigned(value['deadline_monotonic_ms'],store.U64):fail('invalid_response')
    except store.LifecycleStoreError:fail('invalid_response')
    return value

def checked_operation(value,record):
    try:return store.operation(value,record)
    except store.LifecycleStoreError:fail('invalid_response')

def checked_cancel(value,record):
    if not isinstance(value,dict) or set(value)!={'status','operation'} or value['status'] not in ('cancelled','too_late'):fail('invalid_response')
    result=checked_operation(value['operation'],record)
    if (value['status']=='cancelled')!=(result['state']=='cancelled'):fail('invalid_response')
    return result

def execute(args,client,stream):
    from harness_herdr_cli import exact_uuid,read_identity,read_snapshot
    client=_Client(client);database=store.LifecycleStore()
    action=args.command in {'owned-create','owned-interrupt','owned-close'}
    if action:
        method='lifecycle.guarded_'+args.command.removeprefix('owned-')+'.v1'
        source=None
        if args.command=='owned-create':
            device=exact_uuid(args.device)
            cwd=files.read_private_text(args.cwd_file,4096)
            if not cwd.startswith('/'):fail()
        else:
            source=database.read(exact_uuid(args.receipt))
            database.authorize_create(source)
            device=source['device_uuid']
        local_id=read_identity(client)['device_id'];snapshot=read_snapshot(client,local_id,device);epoch=snapshot['server_epoch']
        if not store.text(epoch) or source is not None and source['epoch']!=epoch:fail('lifecycle_operation_unknown')
        record=database.create(str(uuid.uuid4()),device,method,epoch,source)
        record=database.transition(record,phase='prepare_started');emit(stream,safe(record))
        body={'server_epoch':epoch,'owner_key':record['owner_key']}
        if source is None:body['spec']={'kind':'owned_workspace_root','cwd':cwd}
        else:
            receipt=source['create_receipt'];body['receipt']={key:receipt[key] for key in ('receipt_id','generation')}
            if args.command=='owned-close':body['closure']={'members':[receipt['resource_ids']]}
        path=prefix(device,local_id)+'/lifecycle/'
        try:prepared=checked_prepare(client.post_json(path+'prepare',{'method':method,'server_epoch':epoch,'body':body}),record)
        except BaseException:
            try:database.transition(record,phase='prepare_unknown')
            except (store.LifecycleStoreError,store.private.StoreError,OSError):pass
            raise
        record=database.transition(record,phase='prepared',prepared=prepared)
        record,reservation=database.reserve(record,'dispatch');stream.flush()
        result=checked_operation(client.post_json(path+args.command.removeprefix('owned-'),{**body,'admission_ticket':record['ticket']}),record)
        database.observe(record,result,reservation);emit(stream,safe(record,result));return None
    record=database.read(exact_uuid(args.intent))
    if 'operation_uuid' not in record:fail('lifecycle_operation_unknown')
    if args.command=='lifecycle-cancel':database.cancel_allowed(record)
    record,reservation=database.reserve(record,'cancel' if args.command=='lifecycle-cancel' else 'status')
    local_id=read_identity(client)['device_id'];path=prefix(record['device_uuid'],local_id)+'/lifecycle/operation'
    body={'operation_id':record['operation_uuid'],'server_epoch':record['epoch'],'owner_key':record['owner_key']}
    if args.command=='lifecycle-cancel':
        emit(stream,safe(record))
        result=checked_cancel(client.post_json(path+'/cancel',body),record)
    else:result=checked_operation(client.post_json(path,body),record)
    database.observe(record,result,reservation);emit(stream,safe(record,result));return None
