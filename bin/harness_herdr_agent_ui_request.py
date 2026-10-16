"""Codex-only, one-key Web UI request consumer with durable no-replay state."""
import json
import time
import uuid
import harness_herdr_guarded as guarded
import harness_herdr_local as local
import harness_herdr_agent_ui_receipt as receipt

METHODS=["web.agent_ui_request."+name+".v1" for name in ("prepare","dispatch","get","cancel")]
class RequestError(Exception): pass
def fail(code="invalid_agent_ui_request"): raise RequestError(code)
def prefix(device,local_id): return "/api/cli/v1" if device==local_id else f"/api/machines/{device}/cli/v1"
def emit(stream,value): stream.write(json.dumps(value,separators=(",",":"),allow_nan=False)+"\n"); stream.flush()
class Client:
    def __init__(self,raw): self.raw=raw
    def get(self,path,deadline): return self.raw.ui_json("GET",path,deadline_seconds=deadline)
    def post(self,path,body,deadline): return self.raw.ui_json("POST",path,body,deadline)
    def request_json(self,path,deadline_seconds=None): return self.raw.ui_json("GET",path,deadline_seconds=deadline_seconds)
def remaining(deadline):
    value=deadline-time.monotonic()
    if value<=0: raise local.TransportError("deadline_exceeded")
    return value
def capability(client,device,local_id,deadline):
    try: identity=client.get(prefix(device,local_id)+"/identity",remaining(deadline))
    except local.TransportError as error:
        if str(error)=="http_404": fail("guard_not_supported")
        raise
    cap=identity.get("capabilities",{}).get("web_guarded_agent_ui_request_v1") if isinstance(identity,dict) else None
    expected={"version":1,"methods":METHODS,"agents":["codex"],"requests":["interrupt","exit"]}
    if not isinstance(identity,dict) or identity.get("protocol")!=1 or identity.get("device_id")!=device or cap!=expected: fail("guard_not_supported")
def checked(value,record):
    try: return receipt.result(value,record["server_epoch"],record["workflow_id"])
    except receipt.ReceiptError: fail("invalid_response")
def execute(args,raw,stream):
    from harness_herdr_cli import exact_uuid,read_identity
    client=Client(raw); database=receipt.Store(); deadline=time.monotonic()+local.REQUEST_DEADLINE_SECONDS
    if args.command in {"agent-interrupt","agent-exit"}:
        device=exact_uuid(args.device); target=guarded.read_target(args.target_file,device)
        if target["agent_binding"]["detected_agent"]!="codex": fail("agent_ui_request_unsupported")
        local_id=read_identity(client,remaining(deadline))["device_id"]; capability(client,device,local_id,deadline)
        record=database.create(str(uuid.uuid4()),device,"interrupt" if args.command=="agent-interrupt" else "exit",target["server_epoch"])
        emit(stream,{"intent_uuid":record["intent"],"method":METHODS[0]})
        started=database.transition(record,"prepare_started")
        body={"method":METHODS[0],"request":started["method"],"target":target}
        try: response=checked(client.post(prefix(device,local_id)+"/agent/ui-request/prepare",body,remaining(deadline)),started)
        except BaseException:
            database.transition(started,"unknown"); raise
        prepared=database.transition(started,"prepared",workflow_id=response["workflow_id"],safe_result=response)
        dispatch=database.transition(prepared,"dispatch_started")
        try: response=checked(client.post(prefix(device,local_id)+f"/agent/ui-request/workflows/{dispatch['workflow_id']}/dispatch",{"method":METHODS[1],"expected_revision":response["revision"]},remaining(deadline)),dispatch)
        except BaseException:
            database.transition(dispatch,"dispatch_started"); raise
        phase=response["state"] if response["state"] in {"completed","cancelled","stopped"} else "awaiting_operation"
        done=database.transition(dispatch,phase,safe_result=response); emit(stream,{"intent_uuid":done["intent"],**response}); return
    intent=exact_uuid(args.intent); record=database.read(intent)
    if record["workflow_id"] is None: fail("workflow_unknown")
    local_id=read_identity(client,remaining(deadline))["device_id"]; capability(client,record["device_id"],local_id,deadline); base=prefix(record["device_id"],local_id)+f"/agent/ui-request/workflows/{record['workflow_id']}"
    if args.command in {"agent-ui-request-status","status"}:
        response=checked(client.get(base,remaining(deadline)),record); phase="cancel_started" if record["phase"]=="cancel_started" else (response["state"] if response["state"] in {"completed","cancelled","stopped"} else "awaiting_operation"); database.transition(record,phase,safe_result=response); emit(stream,{"intent_uuid":intent,**response}); return
    if record["phase"] in {"prepare_started","unknown","cancel_started"}: fail("reconcile_only")
    started=database.transition(record,"cancel_started")
    try: response=checked(client.post(base+"/cancel",{"method":METHODS[3],"expected_revision":started.get("safe_result",{}).get("revision",0)},remaining(deadline)),started)
    except BaseException: raise
    phase=response["state"] if response["state"] in {"completed","cancelled","stopped"} else "cancel_started"; done=database.transition(started,phase,safe_result=response); emit(stream,{"intent_uuid":intent,**response})
