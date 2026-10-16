"""One explicit Web UI answer action per call; no Native/provider runtime."""
import json
import re
import uuid

import harness_herdr_files as files
import harness_herdr_guarded as guarded
import harness_herdr_local as local
import harness_herdr_ui_receipt as receipt

METHODS = ['web.ui_answer.' + name + '.v1' for name in ('prepare','step','get','cancel')]

class UIAnswerError(Exception):
    def __init__(self, code, tuple_mismatch=False):
        super().__init__(code)
        self.tuple_mismatch = tuple_mismatch


def fail(code='invalid_ui_answer_request'):
    raise UIAnswerError(code)


def option_number(value, maximum):
    # Bound the lexical form before Python int conversion (including 5000 digits).
    limit = str(maximum)
    if not isinstance(value, str) or len(value) > len(limit) or re.fullmatch(r'0|[1-9][0-9]*', value) is None or len(value) == len(limit) and value > limit:
        fail('invalid_selection')
    return int(value)


def emit(stream, value):
    stream.write(json.dumps(value, separators=(',', ':'), allow_nan=False)+'\n')
    stream.flush()


def prefix(device, local_id):
    return '/api/cli/v1' if device == local_id else f'/api/machines/{device}/cli/v1'


def selected_identity(client, device, local_id):
    try: identity = client.request_json(prefix(device, local_id)+'/identity')
    except local.TransportError as error:
        if str(error) == 'http_404': fail('guard_not_supported')
        raise
    if not isinstance(identity, dict) or type(identity.get('protocol')) is not int or identity['protocol'] != 1 or identity.get('device_id') != device:
        fail('guard_not_supported')
    caps = identity.get('capabilities')
    cap = caps.get('web_guarded_ui_answer_v1') if isinstance(caps, dict) else None
    if not isinstance(cap, dict) or set(cap) != {'version','methods','agents'} or type(cap['version']) is not int or cap['version'] != 1 or not isinstance(cap['methods'], list) or len(cap['methods']) != 4 or any(not isinstance(m, str) for m in cap['methods']) or set(cap['methods']) != set(METHODS) or cap['agents'] != ['claude']:
        fail('guard_not_supported')


def checked_workflow(value, record):
    try: return receipt.workflow(value, record['server_epoch'], record['workflow_id'])
    except receipt.ReceiptError:
        mismatch = isinstance(value, dict) and (
            'server_epoch' in value and value['server_epoch'] != record['server_epoch'] or
            record['workflow_id'] is not None and 'workflow_id' in value and value['workflow_id'] != record['workflow_id'])
        raise UIAnswerError('invalid_response', tuple_mismatch=mismatch) from None


class _BoundedClient:
    def __init__(self, client): self.client = client
    def request_json(self, path): return self.client.ui_json('GET', path)
    def post_json(self, path, body): return self.client.ui_json('POST', path, body)


def execute(args, client, stream):
    # Validate caller material before discovery metadata or any HTTP.
    from harness_herdr_cli import exact_uuid, read_identity, read_snapshot
    client = _BoundedClient(client)
    database = receipt.UIReceiptStore()
    if args.command == 'ui-answer-prepare':
        device = exact_uuid(args.device)
        target = guarded.read_target(args.target_file, device)
        prompt = files.read_private_json_object(args.prompt_file, 65536)
        answer = files.read_private_json_object(args.answer_file, 65536)
        if set(prompt) != {'pane_id','prompt_id'} or prompt['pane_id'] != target['pane_id'] or not isinstance(prompt['prompt_id'], str) or re.fullmatch('[0-9a-f]{12}', prompt['prompt_id']) is None: fail()
        if set(answer) != {'option_index'} or not receipt.unsigned(answer['option_index'],32): fail()
        if target['agent_binding']['detected_agent'] != 'claude': fail('ui_answer_unsupported')
        local_id = read_identity(client)['device_id']
        selected_identity(client, device, local_id)
        snapshot = read_snapshot(client, local_id, device)
        if not snapshot['server_epoch'] or snapshot['server_epoch'] != target['server_epoch']: fail('invalid_response')
        record = database.create(str(uuid.uuid4()), device, target['server_epoch'])
        body = {'method':METHODS[0], 'server_epoch':target['server_epoch'], 'target':target, 'prompt':prompt, 'answer':answer}
        return dispatch(client, database, record, prefix(device, local_id)+'/agent/ui-answer/prepare', body, stream, prepare=True)
    intent = exact_uuid(args.intent)
    step = revision = None
    if args.command == 'ui-answer-step':
        step = option_number(args.expected_step, 32)
        revision = option_number(args.expected_revision, receipt.MAX_U64)
    record = database.read(intent)
    if record['workflow_id'] is None: fail('workflow_unknown')
    if args.command == 'ui-answer-step':
        result = record.get('last_result')
        if record['phase'] != 'known' or record['attempt'] is not None or result is None or result['state'] != 'ready' or result['next_step'] != step or result['revision'] != revision:
            fail('reconcile_only')
        if record['local_generation'] == receipt.MAX_U64: fail('receipt_changed')
    if args.command == 'ui-answer-cancel' and record['phase'] in {'prepare_started','cancel_started'}: fail('reconcile_only')
    local_id = read_identity(client)['device_id']
    selected_identity(client, record['device_id'], local_id)
    path = prefix(record['device_id'], local_id)+'/agent/ui-answer/workflows/'+record['workflow_id']
    if args.command == 'ui-answer-status':
        try: result = checked_workflow(client.request_json(path), record)
        except UIAnswerError as error:
            if error.tuple_mismatch:
                # Retain the original locator while refusing every future POST.
                database.transition(record, phase='cancel_started', attempt=record['attempt'])
            raise
        # Pending/lost effects stay permanently get/cancel-only, even after ready.
        phase = 'known' if record['phase'] == 'known' else record['phase']
        database.transition(record, phase=phase, last_result=result, attempt=record['attempt'])
        emit(stream, {'intent_uuid':intent, **result}); return None
    if args.command == 'ui-answer-step':
        record = database.transition(record, phase='step_started', attempt={'step':step,'revision':revision})
        body = {'method':METHODS[1], 'expected_step':step, 'expected_revision':revision}
        path += '/step'
    else:
        record = database.transition(record, phase='cancel_started', attempt=record['attempt'])
        body = {'method':METHODS[3]}; path += '/cancel'
    return dispatch(client, database, record, path, body, stream)


def dispatch(client, database, record, path, body, stream, prepare=False):
    # The persisted reservation and flushed start line precede the single POST.
    emit(stream, {'intent_uuid':record['intent'], 'method':body['method']})
    try:
        result = checked_workflow(client.post_json(path, body), record)
        if record['phase'] == 'step_started' and (result['revision'] <= record['attempt']['revision'] or result['state'] == 'ready' and result['next_step'] != record['attempt']['step']+1):
            fail('invalid_response')
        phase = ('known' if result['state'] == 'ready' else 'uncertain') if prepare or record['phase'] == 'step_started' else 'cancel_started'
        database.transition(record, phase=phase, workflow_id=result['workflow_id'], last_result=result)
    except BaseException as error:
        # A surviving prepare_started/step_started/cancel_started record also bans
        # replay if this best-effort uncertainty persistence fails or is killed.
        phase = 'cancel_started' if record['phase'] == 'cancel_started' or getattr(error, 'tuple_mismatch', False) else 'uncertain'
        try: database.transition(record, phase=phase, attempt=record['attempt'])
        except (receipt.ReceiptError, OSError, guarded.store_module.StoreError): pass
        raise
    emit(stream, {'intent_uuid':record['intent'], **result})
    return None
