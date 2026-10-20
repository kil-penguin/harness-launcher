"""Source-bound historical fallback plus truthful current jq branch proof.

8192 bytes is an accepted-output limit, not a streaming memory bound.
60 seconds is a subprocess invocation timeout, not descendant containment.
The original real inspector and its guards remain unchanged.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

CAPSULE_SHA256 = '7c482fe4ecd13411a89517eab1052fdfb790d240be85eebbf9c26abb3f1db4a6'
SYSTEM_DIRS = ('/usr/bin', '/bin')
HISTORICAL_KEYS = frozenset(('status', 'reason', 'selected', 'physical', 'physical_parent',
    'system_jq', 'manifest_jq_bindings', 'jq_real_query', 'native_calls',
    'non_system_physical_proof', 'guard_or_locator_replacement', 'jq_binary_copy'))
CURRENT_KEYS = HISTORICAL_KEYS | {'preparation_path', 'dependencies'}


class ProofError(Exception):
    """Fixed non-private rejection category; never carries child output."""


def require(condition, label):
    if not condition:
        raise ProofError(label)


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'JSON_DUPLICATE_KEY')
        result[key] = value
    return result


def _constant(_):
    raise ProofError('JSON_NONFINITE')


def decode(raw):
    require(type(raw) is bytes, 'JSON_BYTES_REQUIRED')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError):
        raise ProofError('JSON_INVALID') from None
    require(type(data) is dict, 'JSON_OBJECT_REQUIRED')
    return data


def _absolute(value):
    return (type(value) is str and '\x00' not in value and value.startswith('/')
            and str(PurePosixPath(value)) == value and '..' not in PurePosixPath(value).parts)


def _dependency(value):
    require(type(value) is dict and set(value) == {'name', 'path', 'sha256'}, 'DEPENDENCY_SHAPE')
    require(type(value['name']) is str and value['name'] in ('bash', 'jq', 'python3'), 'DEPENDENCY_NAME')
    require(_absolute(value['path']), 'DEPENDENCY_PATH')
    require(type(value['sha256']) is str and re.fullmatch('[0-9a-f]{64}', value['sha256']) is not None,
            'DEPENDENCY_HASH')


def validate_observation(data, historical=False):
    """Validate each original producer assertion independently; return actual branch."""
    require(type(data) is dict and set(data) == (HISTORICAL_KEYS if historical else CURRENT_KEYS),
            'OBSERVATION_SHAPE')
    selected = data['selected']
    _dependency(selected)
    require(selected['name'] == 'jq', 'SELECTED_NAME')
    require(_absolute(data['physical']) and selected['path'] == data['physical'], 'PHYSICAL_BINDING')
    require(data['physical_parent'] == str(PurePosixPath(data['physical']).parent), 'PHYSICAL_PARENT')
    require(type(data['manifest_jq_bindings']) is list and len(data['manifest_jq_bindings']) > 0,
            'MANIFEST_EMPTY')
    for binding in data['manifest_jq_bindings']:
        _dependency(binding)
        require(binding == selected, 'MANIFEST_BINDING')
    query = data['jq_real_query']
    require(type(query) is dict and set(query) == {'argv', 'exit', 'stdout'}, 'QUERY_SHAPE')
    require(type(query['argv']) is list and query['argv'] ==
            [selected['path'], '-e', '.fixture == true and .n == 7'], 'QUERY_ARGV')
    require(type(query['exit']) is int and query['exit'] == 0 and query['stdout'] == 'true\n', 'QUERY_RESULT')
    require(type(data['native_calls']) is int and data['native_calls'] == 0, 'NATIVE_CALLS')
    require(data['guard_or_locator_replacement'] is False and data['jq_binary_copy'] is False,
            'NO_REPLACEMENT_OR_COPY')
    require(type(data['non_system_physical_proof']) is bool, 'BRANCH_BOOLEAN')
    if historical:
        require(data['status'] == 'PASS' and data['reason'] is None, 'HISTORICAL_STATUS')
    else:
        require(type(data['preparation_path']) is str and len(data['preparation_path']) > 0, 'PREPARATION_SHAPE')
        dependencies = data['dependencies']
        require(type(dependencies) is list and len(dependencies) == 3, 'DEPENDENCIES_SHAPE')
        for dependency in dependencies:
            _dependency(dependency)
        require({x['name'] for x in dependencies} == {'bash', 'jq', 'python3'}, 'DEPENDENCY_NAMES')
        require(next(x for x in dependencies if x['name'] == 'jq') == selected, 'SELECTED_DEPENDENCY')
    if data['non_system_physical_proof'] is True:
        require(data['system_jq'] is None and data['physical_parent'] not in SYSTEM_DIRS, 'NON_SYSTEM_FACTS')
        return 'NON_SYSTEM'
    require(not historical and _absolute(data['system_jq']) and data['system_jq'] == data['physical']
            and data['physical_parent'] in SYSTEM_DIRS, 'SYSTEM_FACTS')
    return 'SYSTEM'


def read_source(root, relative):
    path = root/relative
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), 'SOURCE_REGULAR')
    # One content read per required source. No native/credential/history discovery.
    return path.read_bytes(), f'{stat.S_IMODE(info.st_mode):04o}'


def verify_evidence(root, raw, reader):
    require(type(raw) is bytes and hashlib.sha256(raw).hexdigest() == CAPSULE_SHA256, 'EVIDENCE_DIGEST')
    capsule = decode(raw)
    require(set(capsule) == {'schema', 'provenance', 'observation', 'source_pins'}
            and capsule['schema'] == 'w07.jq-fallback-evidence/v2', 'EVIDENCE_SHAPE')
    require(capsule['provenance'] == {
        'head': '449e1f2776a3555f23ef41cf5f375178a494f5d5',
        'job': 118499176581,
        'whole_log_sha256': '57682781391bbc85ab2c11032f0f9cac5fd17a4484a78fc5d1d328de41fa3f81',
        'line': 243,
        'line_sha256': '8bb1a4784ced6f04655ee53203db99fbc13bb8f0ea418681f249c1580a6c8fc6',
        'source_receipt_sha256': 'e2109915288cf48d6e8050546714ce5e3c2c31c678155729e275a21e4bcf0b72',
    }, 'EVIDENCE_PROVENANCE')
    require(validate_observation(capsule['observation'], historical=True) == 'NON_SYSTEM', 'HISTORICAL_BRANCH')
    pins = capsule['source_pins']
    require(type(pins) is list and len(pins) == 9, 'SOURCE_CLOSURE')
    seen = set()
    for pin in pins:
        # The entire reviewed capsule digest fixes these exact nine path/hash/mode operands.
        require(type(pin) is dict and set(pin) == {'path', 'sha256', 'bytes', 'mode'}
                and pin['path'] not in seen, 'SOURCE_PIN_SHAPE')
        seen.add(pin['path'])
        try:
            body, mode = reader(root, pin['path'])
            valid = (type(body) is bytes and len(body) == pin['bytes']
                     and hashlib.sha256(body).hexdigest() == pin['sha256'] and mode == pin['mode'])
        except Exception:
            raise ProofError('SOURCE_READ_REJECTED') from None
        require(valid, 'SOURCE_CHANGED')
    return hashlib.sha256(json.dumps(pins, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inspect(root, strict, runner):
    argv = [sys.executable, '-I', '-S', '-B', str(root/'test/omp-real-jq-binding.py')]
    if strict:
        argv.append('--require-non-system')
    try:
        result = runner(argv, cwd=root, capture_output=True, timeout=60, check=False)
    except Exception:
        raise ProofError('INSPECTOR_CALL_FAILED') from None
    require(type(result.returncode) is int, 'EXIT_TYPE')
    require(type(result.stdout) is bytes and type(result.stderr) is bytes, 'STREAM_TYPES')
    require(len(result.stdout) <= 8192 and len(result.stderr) <= 8192, 'OUTPUT_LIMIT')
    require(result.stderr == b'', 'STDERR_PRESENT')
    data = decode(result.stdout)
    branch = validate_observation(data)
    expected_exit = 2 if strict and branch == 'SYSTEM' else 0
    require(result.returncode == expected_exit, 'EXIT_MISMATCH')
    require(data['status'] == ('REFUSED' if expected_exit == 2 else 'PASS')
            and data['reason'] == ('NON_SYSTEM_JQ_REQUIRED' if expected_exit == 2 else None), 'STATUS_MISMATCH')
    return data, branch


def prove(root, *, capsule_bytes=None, read_source=read_source, runner=None):
    if capsule_bytes is None:
        capsule_bytes = (root/'test/fixtures/omp-jq-v2-p1-evidence.json').read_bytes()
    source_digest = verify_evidence(root, capsule_bytes, read_source)
    runner = subprocess.run if runner is None else runner
    ordinary, branch = inspect(root, False, runner)
    strict, strict_branch = inspect(root, True, runner)
    require(branch == strict_branch and all(ordinary[field] == strict[field] for field in
            ('selected', 'physical', 'physical_parent', 'system_jq', 'non_system_physical_proof')),
            'CURRENT_PAIR_CHANGED')
    return {'status': 'PASS', 'historical_fallback': 'VERIFIED', 'current_branch': branch,
            'current_non_system_physical_proof': branch == 'NON_SYSTEM',
            'strict_oracle': 'REFUSAL_VERIFIED' if branch == 'SYSTEM' else 'FALLBACK_VERIFIED',
            'native_calls': 0, 'evidence_sha256': CAPSULE_SHA256, 'source_digest': source_digest}


def main():
    try:
        result = prove(Path(__file__).resolve(strict=True).parent.parent)
    except Exception:
        # Never render exceptions, tracebacks or raw producer JSON (even on errors).
        print(json.dumps({'status': 'FAIL', 'reason': 'JQ_CI_PROOF_REJECTED'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
