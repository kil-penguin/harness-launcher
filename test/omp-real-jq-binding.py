"""Prove actual fixture jq binding; CI additionally requires non-system jq."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-non-system', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve(strict=True).parent.parent
    admitted = subprocess.run(
        [sys.executable, '-I', '-S', '-B', str(root/'test/omp-fixture-source.py')],
        capture_output=True, check=True, timeout=10)
    os.environ['HARNESS_TEST_OMP_SOURCE'] = admitted.stdout.decode().strip()
    source = (root/'test/test-omp-continuation.sh').read_text().split(
        "<<'PYTEST'\n", 1)[1].rsplit('\nPYTEST', 1)[0]
    sys.argv = ['fixture', str(root)]
    namespace = {'__name__': 'dependency_binding_fixture'}
    exec(compile(source, 'selected-continuation-fixture', 'exec'), namespace)
    case = namespace['ContinuationTests']('test_C01_C03_C27_public_alias_layer_fake_only')
    case.setUp()
    try:
        case.public_fixture()
        system_jq = shutil.which('jq', path='/usr/bin:/bin')
        with patch.dict(os.environ, {'PATH': case.preparation_path}, clear=False):
            dependencies = case.projection.required_dependencies()
            argv = case.projection.prepare_required(
                str(case.profile), str(case.consumer), str(case.work),
                str(case.binary), str(case.ns), ['--print', '--', 'literal'])
        jq = next(row for row in dependencies if row['name'] == 'jq')
        physical = Path(jq['path']).resolve(strict=True)
        assert str(physical.parent) in case.preparation_path.split(':')
        assert hashlib.sha256(physical.read_bytes()).hexdigest() == jq['sha256']
        bundle = Path(next(value.split('=', 1)[1] for value in argv
                           if value.startswith('--harness-required-bundle=')))
        manifest = json.loads(bundle.read_text())

        def bindings(value):
            if isinstance(value, dict):
                if value.get('name') == 'jq' and 'path' in value and 'sha256' in value:
                    yield value
                for child in value.values():
                    yield from bindings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from bindings(child)

        bound = list(bindings(manifest))
        assert bound and all(row == jq for row in bound)
        query = [jq['path'], '-e', '.fixture == true and .n == 7']
        result = subprocess.run(query, input=b'{"fixture":true,"n":7}',
                                capture_output=True, timeout=5)
        assert result.returncode == 0 and result.stdout == b'true\n' and not result.stderr
        assert not case.calls.exists()
        non_system = system_jq is None and str(physical.parent) not in ('/usr/bin', '/bin')
        refused = args.require_non_system and not non_system
        print(json.dumps({
            'status': 'REFUSED' if refused else 'PASS',
            'reason': 'NON_SYSTEM_JQ_REQUIRED' if refused else None,
            'selected': jq, 'physical': str(physical),
            'physical_parent': str(physical.parent), 'system_jq': system_jq,
            'preparation_path': case.preparation_path, 'dependencies': dependencies,
            'manifest_jq_bindings': bound,
            'jq_real_query': {'argv': query, 'exit': result.returncode,
                              'stdout': result.stdout.decode()},
            'native_calls': 0, 'non_system_physical_proof': non_system,
            'guard_or_locator_replacement': False, 'jq_binary_copy': False,
        }), flush=True)
        return 2 if refused else 0
    finally:
        case.doCleanups()


if __name__ == '__main__':
    sys.exit(main())
