"""Pure CI-proof oracles. Bootstrap reads code/capsule/source9; children are injected."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if 'CAPSULE_BYTES' not in globals():
    CAPSULE_BYTES = (ROOT/'test/fixtures/omp-jq-v1-093-evidence.json').read_bytes()
CAPSULE = json.loads(CAPSULE_BYTES)
if 'SOURCE_INPUTS' not in globals():
    SOURCE_INPUTS = {}
    for item in CAPSULE['source_pins']:
        path = ROOT/item['path']
        SOURCE_INPUTS[item['path']] = (path.read_bytes(), f'{stat.S_IMODE(path.lstat().st_mode):04o}')
if 'SUT' not in globals():
    spec = importlib.util.spec_from_file_location('jq_ci_proof', ROOT/'test/omp-jq-ci-proof.py')
    SUT = importlib.util.module_from_spec(spec)
    with patch('subprocess.run', side_effect=AssertionError('REAL_CHILD_FORBIDDEN')), \
         patch('subprocess.Popen', side_effect=AssertionError('REAL_CHILD_FORBIDDEN')), \
         patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')):
        spec.loader.exec_module(SUT)


def observed(system=True, strict=False):
    """Complete synthetic producer shape, hand-derived from its public contract."""
    path = '/usr/bin/jq' if system else '/opt/homebrew/Cellar/jq/1.8.2/bin/jq'
    selected = {'name': 'jq', 'path': path, 'sha256': 'a'*64}
    return {
        'status': 'REFUSED' if system and strict else 'PASS',
        'reason': 'NON_SYSTEM_JQ_REQUIRED' if system and strict else None,
        'selected': selected, 'physical': path,
        'physical_parent': str(Path(path).parent), 'system_jq': path if system else None,
        'preparation_path': '/synthetic-owned/tools:/usr/bin:/bin',
        'dependencies': [{'name': 'bash', 'path': '/bin/bash', 'sha256': 'b'*64},
                         copy.deepcopy(selected),
                         {'name': 'python3', 'path': '/usr/bin/python3', 'sha256': 'c'*64}],
        'manifest_jq_bindings': [copy.deepcopy(selected)],
        'jq_real_query': {'argv': [path, '-e', '.fixture == true and .n == 7'], 'exit': 0, 'stdout': 'true\n'},
        'native_calls': 0, 'non_system_physical_proof': not system,
        'guard_or_locator_replacement': False, 'jq_binary_copy': False,
    }


def completed(value, exit_code=0, stderr=b''):
    return subprocess.CompletedProcess([], exit_code, json.dumps(value).encode(), stderr)


class HostProofTests(unittest.TestCase):
    # Each negative below catches missing admission/branch/binding/privacy validation,
    # rather than testing a mock's existence or computing expected results via SUT.
    def setUp(self):
        self.guards = [patch('subprocess.run', side_effect=AssertionError('REAL_CHILD_FORBIDDEN')),
                       patch('subprocess.Popen', side_effect=AssertionError('REAL_CHILD_FORBIDDEN')),
                       patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')),
                       patch('pathlib.Path.read_bytes', side_effect=AssertionError('LIVE_FS_FORBIDDEN')),
                       patch('builtins.open', side_effect=AssertionError('LIVE_FS_FORBIDDEN'))]
        for guard in self.guards:
            guard.start()
            self.addCleanup(guard.stop)
        self.calls = []
        self.reads = []

    def invoke(self, ordinary=None, strict=None, sources=None, capsule=None, fault=None):
        normal = completed(observed()) if ordinary is None else ordinary
        restricted = completed(observed(strict=True), 2) if strict is None else strict
        inputs = SOURCE_INPUTS if sources is None else sources

        def read_source(root, relative):
            self.reads.append(relative)
            return inputs[relative]

        def runner(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if fault:
                raise fault
            return restricted if argv[-1] == '--require-non-system' else normal

        return SUT.prove(ROOT, capsule_bytes=CAPSULE_BYTES if capsule is None else capsule,
                         read_source=read_source, runner=runner)

    def refused(self, **kwargs):
        with self.assertRaises(SUT.ProofError) as error:
            self.invoke(**kwargs)
        # Neither exceptions nor result JSON may expose raw child output.
        self.assertNotIn('PRIVATE_SENTINEL', str(error.exception))

    def test_system_binding_keeps_false_and_verifies_strict_refusal(self):
        got = self.invoke()
        self.assertEqual(got['current_branch'], 'SYSTEM')
        self.assertEqual(got['strict_oracle'], 'REFUSAL_VERIFIED')
        self.assertIs(got['current_non_system_physical_proof'], False)
        self.assertEqual(got['historical_fallback'], 'VERIFIED')
        self.assertEqual(got['native_calls'], 0)
        self.assertEqual(len(self.calls), 2)

    def test_genuine_non_system_pair_reports_only_actual_branch(self):
        got = self.invoke(completed(observed(False)), completed(observed(False, True)))
        self.assertEqual(got['current_branch'], 'NON_SYSTEM')
        self.assertEqual(got['strict_oracle'], 'FALLBACK_VERIFIED')
        self.assertIs(got['current_non_system_physical_proof'], True)

    def test_exact_argv_cwd_deadline_order_and_read_once(self):
        self.invoke()
        first = [sys.executable, '-I', '-S', '-B', str(ROOT/'test/omp-real-jq-binding.py')]
        self.assertEqual([x[0] for x in self.calls], [first, first+['--require-non-system']])
        for _, kwargs in self.calls:
            self.assertEqual(kwargs, {'cwd': ROOT, 'capture_output': True, 'timeout': 60, 'check': False})
        self.assertEqual(len(self.reads), 9)
        self.assertEqual(len(set(self.reads)), 9)

    def test_ordinary_failure_shortcircuits_strict(self):
        self.refused(ordinary=completed(observed(), 1))
        self.assertEqual(len(self.calls), 1)

    def test_each_of_nine_source_mutations_stops_before_child(self):
        for path in SOURCE_INPUTS:
            with self.subTest(path=path):
                inputs = dict(SOURCE_INPUTS)
                body, mode = inputs[path]
                inputs[path] = (body+b'\nchanged', mode)
                self.calls.clear()
                self.refused(sources=inputs)
                self.assertEqual(len(self.calls), 0)

    def test_missing_source_and_mode_mutation_are_not_blessed_by_metadata(self):
        path = next(iter(SOURCE_INPUTS))
        for missing in (True, False):
            inputs = dict(SOURCE_INPUTS)
            if missing:
                del inputs[path]
            else:
                inputs[path] = (inputs[path][0], '0777')
            self.calls.clear()
            self.refused(sources=inputs)
            self.assertEqual(len(self.calls), 0)

    def test_capsule_tamper_or_missing_evidence_stops_before_child(self):
        for capsule in (b'', b'{}', CAPSULE_BYTES+b' ', CAPSULE_BYTES.replace(b'"native_calls": 0', b'"native_calls": 1')):
            with self.subTest(size=len(capsule)):
                self.calls.clear()
                self.refused(capsule=capsule)
                self.assertEqual(len(self.calls), 0)

    def test_historical_semantics_are_independently_validated(self):
        original = CAPSULE['observation']
        mutations = [('status', 'REFUSED'), ('reason', 'x'), ('system_jq', '/usr/bin/jq'),
                     ('non_system_physical_proof', False), ('native_calls', 1),
                     ('jq_binary_copy', True), ('guard_or_locator_replacement', True),
                     ('manifest_jq_bindings', []), ('jq_real_query', {'argv': [], 'exit': 0, 'stdout': 'true\n'})]
        for field, value in mutations:
            with self.subTest(field=field):
                data = copy.deepcopy(original)
                data[field] = value
                with self.assertRaises(SUT.ProofError):
                    SUT.validate_observation(data, historical=True)

    def test_status_reason_exit_matrix_is_fail_closed(self):
        for exit_code in (0, 1, -1, True, 3):
            with self.subTest(exit=exit_code):
                self.refused(strict=completed(observed(strict=True), exit_code))
        for field, value in [('status', 'PASS'), ('reason', None), ('non_system_physical_proof', True)]:
            data = observed(strict=True)
            data[field] = value
            self.refused(strict=completed(data, 2))
        for exit_code in (2, 1):
            self.refused(ordinary=completed(observed(False)), strict=completed(observed(False), exit_code))

    def test_mixed_path_boolean_null_combinations_fail(self):
        for field, value in [('system_jq', None), ('physical_parent', '/opt/homebrew/bin'),
                             ('non_system_physical_proof', True), ('physical', '/bin/jq')]:
            data = observed()
            data[field] = value
            self.refused(ordinary=completed(data))
        data = observed(False)
        data['system_jq'] = '/usr/bin/jq'
        self.refused(ordinary=completed(data))

    def test_per_result_hash_manifest_query_and_native_checks(self):
        changes = [
            ('native_calls', True), ('native_calls', 1), ('native_calls', '0'),
            ('non_system_physical_proof', 0), ('jq_binary_copy', 0),
            ('guard_or_locator_replacement', 0), ('jq_binary_copy', True),
            ('manifest_jq_bindings', []), ('manifest_jq_bindings', [observed()['selected'], {}]),
            ('selected', {'name': 'jq', 'path': '/usr/bin/jq', 'sha256': 'invalid'}),
            ('jq_real_query', {'argv': ['/usr/bin/jq', '-e', '.fixture == true and .n == 7'], 'exit': True, 'stdout': 'true\n'}),
            ('jq_real_query', {'argv': ['/usr/bin/jq', '-e', 'different'], 'exit': 0, 'stdout': 'true\n'}),
            ('jq_real_query', {'argv': ['/usr/bin/jq', '-e', '.fixture == true and .n == 7'], 'exit': 0, 'stdout': 'false\n'}),
            ('dependencies', []), ('preparation_path', None),
        ]
        for field, value in changes:
            for phase in ('ordinary', 'strict'):
                with self.subTest(field=field, phase=phase, kind=type(value).__name__):
                    data = observed(strict=phase == 'strict')
                    data[field] = value
                    self.refused(**{phase: completed(data, 2 if phase == 'strict' else 0)})

    def test_cross_call_hash_or_path_change_is_rejected(self):
        data = observed(strict=True)
        data['selected']['sha256'] = 'd'*64
        data['manifest_jq_bindings'][0]['sha256'] = 'd'*64
        data['dependencies'][1]['sha256'] = 'd'*64
        self.refused(strict=completed(data, 2))
        self.refused(strict=completed(observed(False), 0))

    def test_output_shape_duplicate_keys_and_trailing_data_fail(self):
        raw_cases = [b'', b'{', b'[]', b'null', b'{}{}', b'{"x":1,"x":2}', b'\xff', b'{"x":NaN}']
        for raw in raw_cases:
            for strict in (False, True):
                with self.subTest(raw_size=len(raw), strict=strict):
                    result = subprocess.CompletedProcess([], 2 if strict else 0, raw, b'')
                    self.refused(**{'strict' if strict else 'ordinary': result})
        data = observed()
        data['unexpected_private_field'] = 'PRIVATE_SENTINEL'
        self.refused(ordinary=completed(data))
        data = observed()
        del data['selected']
        self.refused(ordinary=completed(data))

    def test_exact_8192_limit_accepted_8193_rejected(self):
        normal = json.dumps(observed()).encode()
        exact = normal+b' '*(8192-len(normal))
        self.invoke(ordinary=subprocess.CompletedProcess([], 0, exact, b''))
        self.refused(ordinary=subprocess.CompletedProcess([], 0, exact+b' ', b''))
        strict = json.dumps(observed(strict=True)).encode()
        exact = strict+b' '*(8192-len(strict))
        self.invoke(strict=subprocess.CompletedProcess([], 2, exact, b''))
        self.refused(strict=subprocess.CompletedProcess([], 2, exact+b' ', b''))

    def test_stderr_and_wrong_stream_types_fail_without_raw_output(self):
        for err in (b'PRIVATE_SENTINEL', b'x'*8192, b'x'*8193):
            self.refused(ordinary=completed(observed(), stderr=err))
            self.refused(strict=completed(observed(strict=True), 2, err))
        self.refused(ordinary=subprocess.CompletedProcess([], 0, '{}', b''))
        self.refused(ordinary=subprocess.CompletedProcess([], 0, b'{}', None))

    def test_timeout_or_unexpected_exception_stops_once_and_sanitizes(self):
        for error in (subprocess.TimeoutExpired(['PRIVATE_SENTINEL'], 60, output=b'PRIVATE_SENTINEL'),
                      OSError('PRIVATE_SENTINEL'), RuntimeError('PRIVATE_SENTINEL')):
            self.calls.clear()
            self.refused(fault=error)
            self.assertEqual(len(self.calls), 1)

    def test_report_and_capsule_never_publish_private_fixture_data(self):
        result = json.dumps(self.invoke())
        for forbidden in ('/synthetic-owned/', 'preparation_path', 'dependencies', 'PRIVATE_SENTINEL'):
            self.assertNotIn(forbidden, result)
            self.assertNotIn(forbidden, CAPSULE_BYTES.decode())
        self.assertEqual(set(CAPSULE['observation']), {'status', 'reason', 'selected', 'physical',
                         'physical_parent', 'system_jq', 'manifest_jq_bindings', 'jq_real_query',
                         'native_calls', 'non_system_physical_proof', 'guard_or_locator_replacement', 'jq_binary_copy'})


if __name__ == '__main__':
    unittest.main()
