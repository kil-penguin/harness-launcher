import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "harness_launch_context.py"
spec = importlib.util.spec_from_file_location("harness_launch_context", SCRIPT)
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)

CLEAN = {"HOME": "/Users/u", "PATH": "/usr/bin"}
# 40 python launcher -> 30 zsh pane shell -> 20 herdr server -> 1 launchd
TOP = {40: (30, "/usr/bin/python3"), 30: (20, "-zsh"), 20: (1, "/opt/homebrew/bin/herdr")}


class LaunchContextTest(unittest.TestCase):
    def check(self, environ=CLEAN, table=TOP, start=40, tty=""):
        return context.launch_top_level(environ=environ, table=table, start_pid=start, tty_reason=tty)

    def test_terminal_launch_under_a_pane_shell_is_top_level(self):
        self.assertEqual(self.check(), (True, None))

    def test_agent_ancestor_is_not_top_level(self):
        for name in ("claude", "codex", "/opt/homebrew/bin/node"):
            table = dict(TOP)
            table[30] = (25, "/bin/zsh")
            table[25] = (20, name)
            with self.subTest(name=name):
                self.assertEqual(self.check(table=table), (False, "agent_ancestor"))

    def test_reparented_orphan_without_foreground_terminal_is_not_top_level(self):
        orphan = {40: (1, "/usr/bin/python3")}
        self.assertEqual(self.check(table=orphan, tty="background"), (False, "background"))
        self.assertEqual(self.check(table=orphan, tty="no_tty"), (False, "no_tty"))

    def test_missing_pid_and_hop_limit_are_unknown(self):
        self.assertEqual(self.check(table={40: (30, "python3")}), (False, "pid_missing"))
        chain = {pid: (pid + 1, "/bin/sh") for pid in range(100, 166)}
        self.assertEqual(self.check(table=chain, start=100), (False, "hop_limit"))
        self.assertEqual(context.ancestry_reason(None, 40), "ps_unreadable")

    def test_each_agent_marker_withholds(self):
        for name in context.AGENT_MARKERS:
            with self.subTest(name=name):
                self.assertEqual(self.check(environ=dict(CLEAN, **{name: "1"})), (False, "env:" + name))
        self.assertEqual(self.check(environ=dict(CLEAN, CLAUDECODE="")), (True, None))

    def test_cli_reports_reason_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = pathlib.Path(tmp) / "ps"
            table.write_text("40 30 python3\n30 1 claude\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in context.AGENT_MARKERS}
            done = subprocess.run([sys.executable, str(SCRIPT), "top-level", "--pstable", str(table), "--start-pid", "40"],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 1)
            self.assertEqual(done.stdout.strip(), "top_level=0 reason=no_tty")
            bad = subprocess.run([sys.executable, str(SCRIPT), "top-level", "--bogus", "1"], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
