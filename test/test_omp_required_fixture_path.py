#!/usr/bin/env python3
"""Required-loading fixture preparation PATH regression, real jq only.

Breaks caught: a broad shared-bin alias leaking unrelated tools, missing real jq
being ignored, caller PATH overriding system-first selection, and shadow-oracle
weakening. Only locator observations are injected; no fake/copied executable.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
LOADING=ROOT/'test/test-omp-required-loading.sh'
REAL_JQ=shutil.which('jq',path='/usr/bin:/bin') or shutil.which('jq')
if REAL_JQ is None:
    raise SystemExit('required-loading path regression requires real jq')
REAL_JQ=Path(REAL_JQ).resolve(strict=True)
# Load the exact original fixture's Python body without running unittest.main.
body=LOADING.read_text().split("<<'PYTEST'\n",1)[1].rsplit('\nPYTEST',1)[0]
tree=ast.parse(body,filename=str(LOADING))
last=tree.body[-1]
assert isinstance(last,ast.Expr) and isinstance(last.value,ast.Call)
assert isinstance(last.value.func,ast.Attribute) and last.value.func.attr=='main'
tree.body.pop()
namespace={'__name__':'required_loading_fixture_subject','__file__':str(LOADING)}
with patch.object(sys,'argv',[str(LOADING),str(ROOT)]):
    exec(compile(tree,str(LOADING),'exec'),namespace)
RequiredLoading=namespace['RequiredLoading']

class RequiredFixturePath(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='required-path-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.shared=self.root/'shared-bin';self.shared.mkdir()
        self.alias=self.shared/'jq';self.alias.symlink_to(REAL_JQ)
        self.lookup=[]
        self.original_which=shutil.which
    def fixture(self,system):
        case=RequiredLoading('test_normal_entry_prepares_policy_and_selected_skill_without_native_admission')
        self.addCleanup(case.doCleanups)
        def which(name,mode=os.F_OK|os.X_OK,path=None):
            if name!='jq':return self.original_which(name,mode=mode,path=path)
            self.lookup.append(path)
            if path=='/usr/bin:/bin':return str(REAL_JQ) if system else None
            return str(self.alias)
        with patch.object(shutil,'which',side_effect=which):case.setUp()
        return case
    def test_system_first_real_selection_does_not_consult_caller_alias(self):
        case=self.fixture(True)
        self.assertEqual(self.lookup,['/usr/bin:/bin'])
        self.assertNotIn('/opt/homebrew/bin',case.env['PATH'].split(':'))
        self.assertNotIn(str(self.shared),case.env['PATH'].split(':'))
    def test_fallback_symlink_exposes_only_actual_physical_jq_parent(self):
        case=self.fixture(False)
        self.assertEqual(self.lookup,['/usr/bin:/bin',None])
        self.assertNotIn(str(self.shared),case.env['PATH'].split(':'))
        self.assertNotIn('/opt/homebrew/bin',case.env['PATH'].split(':'))
        self.assertIn(str(REAL_JQ.parent),case.env['PATH'].split(':'))
        self.assertEqual(set(case.env['PATH'].split(':'))-{str(REAL_JQ.parent)},
                         {'/usr/bin','/bin'}-{str(REAL_JQ.parent)})
    def test_missing_real_jq_refuses_during_fixture_setup(self):
        case=RequiredLoading('test_normal_entry_prepares_policy_and_selected_skill_without_native_admission')
        self.addCleanup(case.doCleanups)
        with patch.object(shutil,'which',return_value=None):
            with self.assertRaisesRegex(AssertionError,'requires real jq'):case.setUp()
    def test_real_production_binding_hash_query_and_native_zero(self):
        case=self.fixture(False)
        with patch.dict(os.environ,{'PATH':case.env['PATH']}):
            deps=case.p.required_dependencies()
        jq=next(item for item in deps if item['name']=='jq')
        self.assertEqual(Path(jq['path']).resolve(strict=True),REAL_JQ)
        self.assertEqual(jq['sha256'],hashlib.sha256(REAL_JQ.read_bytes()).hexdigest())
        result=subprocess.run([str(REAL_JQ),'-n','{value: (6*7)}'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'value':42})
        self.assertFalse(case.capture.exists())
    def test_unchanged_shadow_guard_rejects_injected_resolution_of_real_other_shell(self):
        case=self.fixture(False)
        original=shutil.which
        def observed(name,mode=os.F_OK|os.X_OK,path=None):
            if name=='bash' and path is not None:return '/bin/sh'
            return original(name,mode=mode,path=path)
        with patch.dict(os.environ,{'PATH':case.env['PATH']}),patch.object(shutil,'which',side_effect=observed):
            with self.assertRaises(case.p.ProjectionError) as raised:case.p.required_dependencies()
        self.assertEqual(raised.exception.label,'OMP_REQUIRED_DEPENDENCY_SHADOW')
        self.assertFalse(case.capture.exists())
    def test_only_preparation_path_changes_existing_environment_values(self):
        case=self.fixture(False)
        env=dict(case.env);env.pop('PATH')
        self.assertEqual(env,{'HOME':str(case.home),'LC_ALL':'C','TERM':'dumb',
                             'HARNESS_CLAUDE_PLUGIN_MIRROR':'0','PYTHONDONTWRITEBYTECODE':'1'})

    def test_original_normal_entry_and_readonly_consumers_share_prepared_path(self):
        # The caller sees only an owned alias to real jq. Fixture preparation
        # must reach all three original consumers, not only the entry child.
        with patch.dict(os.environ, {'PATH': str(self.shared)}):
            case = self.fixture(False)
            try:
                self.assertNotIn(str(self.shared), os.environ['PATH'].split(':'))
                self.assertEqual(os.environ['PATH'], case.env['PATH'])
                case.test_normal_entry_prepares_policy_and_selected_skill_without_native_admission()
            finally:
                case.doCleanups()
            self.assertEqual(os.environ['PATH'], str(self.shared))

    def test_parent_preparation_path_restores_caller_after_exception(self):
        before = dict(os.environ)
        with patch.dict(os.environ, {'PATH': str(self.shared)}):
            case = self.fixture(False)
            try:
                with self.assertRaisesRegex(RuntimeError, 'fixture cleanup control'):
                    raise RuntimeError('fixture cleanup control')
            finally:
                case.doCleanups()
            self.assertEqual(os.environ['PATH'], str(self.shared))
        self.assertEqual(dict(os.environ), before)

def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(RequiredFixturePath)

if __name__=='__main__':unittest.main(verbosity=2)
