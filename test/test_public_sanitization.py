#!/usr/bin/env python3
"""Exercise the real embedded public scanner with owned temporary text inputs."""

import ast
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


RELEASE = '18.11.3-kh.w07.1'
# Literal observations from the seven original public source files.
ACTUAL_CONTEXTS = (
    ('actual-1-306', 'is `18.11.3-kh.w07.1`; source revision, patch/build provenance and actual binary'),
    ('actual-2-270', "REQUIRED_VERSION = '18.11.3-kh.w07.1'"),
    ('actual-3-288', '`18.11.3-kh.w07.1`, reviewed native source revision/patch/build provenance and actual'),
    ('actual-4-1122', 'OMP_NATIVE_VERSION = "18.11.3-kh.w07.1"'),
    ('actual-5-82', "    version === '18.11.3-kh.w07.1' && release.upstream_base_version === '18.11.3' &&"),
    ('actual-5-198', "          manifest.runtime_version === '18.11.3-kh.w07.1' &&"),
    ('actual-6-379', "        self.release={'version':'18.11.3-kh.w07.1','source_revision':'1'*40,"),
    ('actual-7-72', "assert m['schema']==2 and m['runtime_version']=='18.11.3-kh.w07.1'"),
    ('actual-7-99', "             '2099-01-01T00:00:00Z',{'version':'18.11.3-kh.w07.1','source_revision':'1'*40,"),

)


def load_scanner():
    wrapper = Path(__file__).with_name('test-public-sanitization.sh')
    shell = wrapper.read_text(encoding='utf-8')
    prefix = 'python3 - "$ROOT" <<' + "'PY'" + '\n'
    before, marker, remainder = shell.partition(prefix)
    body, end, after = remainder.partition('\nPY\n')
    if not marker or not end:
        raise AssertionError('embedded scanner body not found')
    tree = ast.parse(body, filename=str(wrapper))
    assignments = [node for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name)
                           and target.id in ('aliases', 'private_names')
                           for target in node.targets)]
    policy = {'__builtins__': {}}
    exec(compile(ast.Module(body=assignments, type_ignores=[]),
                 str(wrapper), 'exec'), policy)
    return compile(tree, str(wrapper), 'exec'), policy


def oracle_cases(aliases, private_names):
    cases = [(key, text, 'ALLOW', 'actual_7file_public_release_context')
             for key, text in ACTUAL_CONTEXTS]
    for key, text in (
        ('token', RELEASE),
        ('quoted', "'" + RELEASE + "'"),
        ('repeat', RELEASE + ' ' + RELEASE),
        ('json', '{"version": "' + RELEASE + '"}'),
    ):
        cases.append(('public-' + key, text, 'ALLOW',
                      'explicit_public_token_positive'))
    for index, alias in enumerate(aliases):
        for key, text in (
            ('bare', alias), ('upper', alias.upper()),
            ('title', alias.title()), ('profile', 'profile=' + alias),
            ('path', '/Users/' + alias + '/private'),
            ('before', alias + ' ' + RELEASE),
            ('after', RELEASE + ' ' + alias),
            ('punctuated', RELEASE + ',' + alias),
            ('underscore', '_' + alias + '_'),
        ):
            cases.append((f'private-{index}-{key}', text, 'DENY',
                          'standalone_alias_private_context_preserved'))
        for key, text in (('prefix', '2' + alias), ('suffix', alias + '2')):
            cases.append((f'legacy-{index}-alnum-{key}', text, 'ALLOW',
                          'original_ascii_boundary_control'))
    unapproved = (
        'x' + RELEASE, RELEASE + 'x', '_' + RELEASE, RELEASE + '_',
        RELEASE + '.extra', RELEASE + '-private', RELEASE + '0',
        RELEASE.replace('18.11.3', '18.11.4'),
        RELEASE.replace(aliases[0], aliases[0].upper()), RELEASE[:-2],
        RELEASE.replace(aliases[0], aliases[1]),
        RELEASE.replace(aliases[0], aliases[2]),
        '字' + RELEASE, RELEASE + '字',
    )
    for index, text in enumerate(unapproved):
        cases.append((f'unapproved-release-{index}', text, 'DENY',
                      'exact_token_case_boundary_or_version_negative'))
    for index, name in enumerate(private_names):
        cases.append((f'company-{index}', RELEASE + ' ' + name, 'DENY',
                      'private_company_detector_preserved'))
    cases.append(('ordinary-text', 'ordinary public text', 'ALLOW', 'unchanged_neutral_control'))
    return cases


class PublicSanitizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner, cls.policy = load_scanner()

    def scan_files(self, contents, listing=None):
        with tempfile.TemporaryDirectory(prefix='public-sanitization-') as temp:
            root = Path(temp)
            for relative, text in contents.items():
                if text is None:
                    continue
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text if isinstance(text, bytes)
                                   else text.encode('utf-8'))
            paths = list(contents) if listing is None else listing
            encoded = ('\0'.join(paths) + '\0').encode('utf-8')
            output = io.StringIO()
            status = 0
            # Only file enumeration is replaced; the real scanner reads each file.
            with mock.patch('subprocess.check_output', return_value=encoded) as listing_call:
                with mock.patch.object(sys, 'argv', ['-', str(root)]):
                    with contextlib.redirect_stdout(output):
                        try:
                            exec(self.scanner, {'__name__': '__main__'})
                        except SystemExit as exc:
                            status = exc.code
                listing_call.assert_called_once_with([
                    'git', '-C', str(root), 'ls-files', '--cached', '--others',
                    '--exclude-standard', '-z'])
            return status, output.getvalue()

    def test_frozen_oracle_families(self):
        for key, text, expected, group in oracle_cases(
                self.policy['aliases'], self.policy['private_names']):
            with self.subTest(case=key):
                status, output = self.scan_files({'fixture.txt': text})
                self.assertEqual(status, 0 if expected == 'ALLOW' else 1)
                if expected == 'ALLOW':
                    self.assertEqual(output, 'PASS: tracked public files contain no '
                                     'private profile aliases or company names\n')
                else:
                    category = ('private company name' if key.startswith('company-')
                                else 'private profile alias')
                    self.assertIn('fixture.txt: ' + category + '\n', output)

    def test_scanner_and_unit_source_are_included(self):
        here = Path(__file__).parent
        status, output = self.scan_files({
            'test/test-public-sanitization.sh':
                (here / 'test-public-sanitization.sh').read_text(encoding='utf-8'),
            'test/test_public_sanitization.py':
                Path(__file__).read_text(encoding='utf-8'),
        })
        self.assertEqual(status, 0, output)

    def test_original_read_errors_sorted_unique_full_text_findings(self):
        alias = self.policy['aliases'][0]
        company = self.policy['private_names'][0]
        status, output = self.scan_files({
            'z.txt': RELEASE + ' ' + alias + ' ' + company,
            'a.txt': company + ' ' + RELEASE,
            'binary.dat': b'\xff',
            'missing.txt': None,
        }, listing=['z.txt', 'a.txt', 'z.txt', 'binary.dat', 'missing.txt'])
        self.assertEqual(status, 1)
        self.assertEqual(output, 'a.txt: private company name\n'
                         'z.txt: private company name\n'
                         'z.txt: private profile alias\n')


if __name__ == '__main__':
    unittest.main()
