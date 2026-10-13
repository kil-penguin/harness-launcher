"""Headless run-start callback for P3-L: Buzz capture stays off unless both journal arguments are given."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('headless_journal_under_test', ROOT / 'bin/harness_headless.py')
hh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hh)


def args(**kw):
    ns = argparse.Namespace(journal_board=None, journal_task=None)
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


class JournalRunStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.hook = self.home / '.local/share/harness-service/bin/harness-session-hook'
        self.hook.parent.mkdir(parents=True)
        self.log = self.home / 'log'

    def install(self, body):
        self.hook.write_text('#!/bin/sh\n' + body)
        self.hook.chmod(0o755)

    def test_parse_accepts_the_two_arguments(self):
        ns = hh.parse_args(['h', '--prompt-file', 'p', '--result-file', 'r', '--lock-file', 'l', '--budget-usd', '1',
                            '--timeout-min', '1', '--journal-board', 'main', '--journal-task', 't_0a1b2c3d'])
        self.assertEqual((ns.journal_board, ns.journal_task), ('main', 't_0a1b2c3d'))

    def test_no_arguments_means_no_callback(self):
        self.install('echo called > "%s"\n' % self.log)
        self.assertIsNone(hh.journal_run_start(args(), str(self.home)))
        self.assertFalse(self.log.exists())

    def test_both_arguments_run_the_callback_with_a_run_start_payload(self):
        self.install('{ echo "$*"; cat; } > "%s"\necho noisy\nexit 3\n' % self.log)
        hh.journal_run_start(args(journal_board='main', journal_task='t_0a1b2c3d'), str(self.home))
        lines = self.log.read_text().splitlines()
        self.assertEqual(lines[0], '--runtime buzz')
        payload = json.loads(lines[1])
        self.assertEqual((payload['hook_event_name'], payload['board'], payload['task']), ('RunStart', 'main', 't_0a1b2c3d'))
        self.assertRegex(payload['run_id'], r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
        self.assertEqual(set(payload), {'hook_event_name', 'run_id', 'board', 'task'})

    def test_missing_callback_is_silent(self):
        self.assertFalse(self.hook.exists())
        hh.journal_run_start(args(journal_board='main', journal_task='t_0a1b2c3d'), str(self.home))

    def test_hanging_callback_is_cut_off(self):
        self.install('exec sleep 30\n')
        self.addCleanup(setattr, hh, 'JOURNAL_TIMEOUT', hh.JOURNAL_TIMEOUT)
        hh.JOURNAL_TIMEOUT = 1
        start = time.monotonic()
        hh.journal_run_start(args(journal_board='main', journal_task='t_0a1b2c3d'), str(self.home))
        self.assertLess(time.monotonic() - start, 5)

    def test_arguments_come_as_a_valid_pair(self):
        for bad in (dict(journal_board='main'), dict(journal_task='t_0a1b2c3d'),
                    dict(journal_board='Main', journal_task='t_0a1b2c3d'),
                    dict(journal_board='main', journal_task='t_XYZ')):
            with self.subTest(bad=bad), self.assertRaises(hh.Refused):
                hh.validate_journal_args(args(**bad))
        hh.validate_journal_args(args())
        hh.validate_journal_args(args(journal_board='main', journal_task='t_0a1b2c3d'))


if __name__ == '__main__':
    unittest.main()
