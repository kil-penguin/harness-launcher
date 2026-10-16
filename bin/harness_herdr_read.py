"""Explicit bounded visible pane text; ordinary read returns metadata only."""
import re
import time
import harness_herdr_guarded as guarded
import harness_herdr_local as local

class ReadError(Exception):pass

def fail(code='invalid_response'):raise ReadError(code)

def number(value,maximum):
    limit=str(maximum)
    if not isinstance(value,str) or len(value)>len(limit) or re.fullmatch(r'[1-9][0-9]*',value) is None or len(value)==len(limit) and value>limit:fail('invalid_selection')
    return int(value)

class _BoundedClient:
    def __init__(self,client):self.client=client;self.deadline=time.monotonic()+local.REQUEST_DEADLINE_SECONDS
    def request_json(self,path):return self.client.pane_read_json('GET',path,deadline_seconds=local.remaining(self.deadline))
    def read(self,path,body):return self.client.pane_read_json('POST',path,body,deadline_seconds=local.remaining(self.deadline))

def execute(args,client):
    from harness_herdr_cli import exact_uuid,valid_id,read_identity,read_snapshot,panes
    device=exact_uuid(args.device)
    if not valid_id(args.pane):fail('invalid_selection')
    if not args.body and (args.lines is not None or args.max_bytes is not None):fail('invalid_selection')
    lines=40 if args.lines is None else number(args.lines,200)
    maximum=4096 if args.max_bytes is None else number(args.max_bytes,8192)
    client=_BoundedClient(client);local_id=read_identity(client)['device_id'];snapshot=read_snapshot(client,local_id,device)
    rows=[row for row in snapshot['panes'] if row['pane_id']==args.pane]
    if len(rows)!=1:fail('invalid_selection')
    row=rows[0]
    if not args.body:return panes([row],row['workspace_id'],row['tab_id'])[0]
    epoch=snapshot['server_epoch']
    if not guarded._text(epoch):fail()
    selectors={'server_epoch':epoch,**{key:row[key] for key in ('workspace_id','tab_id','pane_id','terminal_id')}}
    prefix='/api/cli/v1' if device==local_id else f'/api/machines/{device}/cli/v1'
    result=client.read(prefix+'/pane/read',{**selectors,'lines':lines,'max_bytes':maximum})
    expected={'device_id':device,**selectors}
    if not isinstance(result,dict) or set(result)!=set(expected)|{'text','truncated'} or any(result[key]!=value for key,value in expected.items()) or type(result['truncated']) is not bool or not isinstance(result['text'],str):fail()
    try:
        if len(result['text'].encode('utf-8'))>maximum:fail()
    except UnicodeError:fail()
    return result
