#!/usr/bin/env python3
"""harness-herdr-web: run herdr web ui local-only behind a token.

Upstream (devswha/herdr-web-ui) serves token-less local callers with full
access. The invariant kept here: an effective token of at least 32 characters,
HOST=127.0.0.1, no Tailscale login on the machine (unsupported for now), and a
running server that rejects an unauthenticated probe.
See docs/herdr-web-ui.md.
"""

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PLUGIN_REPO = "devswha/herdr-web-ui"
PLUGIN_ID = "devswha.herdr-web-ui"
PINNED_REF = "v0.3.34"
PINNED_COMMIT = "efd017b1a42cc090fb0fec4221e160554b1d407a"
DEFAULT_PORT = 7317
MIN_TOKEN_LEN = 32
MIN_BUN = (1, 4, 0)
LOOPBACK = "127.0.0.1"
TOKEN_KEY = "HERDR_WEB_TOKEN"
HOST_KEY = "HOST"
UPDATE_KEY = "HERDR_WEB_AUTO_UPDATE"
MANAGED = (TOKEN_KEY, HOST_KEY, UPDATE_KEY)
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
RESTART_HINT = (f"herdr plugin action invoke {PLUGIN_ID}.stop, "
                f"then herdr plugin action invoke {PLUGIN_ID}.start")


class Report:
    def __init__(self):
        self.failed = False

    def ok(self, text):
        print(f"OK   {text}")

    def warn(self, text):
        print(f"WARN {text}")

    def fail(self, text):
        self.failed = True
        print(f"FAIL {text}")


class Refused(Exception):
    """A precondition that makes writing or trusting the config unsafe."""


def run(argv, input=None, timeout=60):
    if argv[0] == "herdr":
        # herdr runs plugins and status commands with a PATH that may lack it.
        argv = [os.environ.get("HERDR_BIN_PATH") or "herdr"] + argv[1:]
    return subprocess.run(argv, capture_output=True, text=True, input=input, timeout=timeout)


# --- upstream env grammar (scripts/plugin.ts) --------------------------------

# JavaScript String.prototype.trim() (ECMA-262 WhiteSpace + LineTerminator): it
# strips U+FEFF, and not the \x1c-\x1f / \x85 that Python's str.strip() adds.
JS_SPACE = ("\t\n\x0b\x0c\r \xa0        "
            "        　﻿")


def parse_line(line):
    line = line.strip(JS_SPACE)
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key, value = key.strip(JS_SPACE), value.strip(JS_SPACE)
    if value[:1] in ("'", '"'):
        value = value[1:]
    if value[-1:] in ("'", '"'):
        value = value[:-1]
    return key, value


def parse(text):
    values = {}
    for line in text.split("\n"):
        parsed = parse_line(line)
        if parsed:
            values[parsed[0]] = parsed[1]
    return values


# --- files -------------------------------------------------------------------

def config_dir():
    result = run(["herdr", "plugin", "config-dir", PLUGIN_ID])
    path = result.stdout.strip()
    if result.returncode != 0 or not path:
        raise Refused("herdr plugin config-dir failed; is herdr installed?")
    return Path(path)


def owned(path, want_dir):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise Refused(f"{path} is a symlink")
    if info.st_uid != os.getuid():
        raise Refused(f"{path} is not owned by you")
    if want_dir != stat.S_ISDIR(info.st_mode):
        raise Refused(f"{path} is not a {'directory' if want_dir else 'regular file'}")
    return info


def files(directory):
    owned(directory, want_dir=True)
    env, dot = directory / "env", directory / ".env"
    for path in (env, dot):
        if path.exists() or path.is_symlink():
            owned(path, want_dir=False)
    return env, dot


def read(path):
    # Like Bun's file.text(): no newline translation (upstream splits on \n
    # only, so a lone \r does not end a line) and U+FFFD for invalid UTF-8.
    if not path.exists():
        return ""
    with open(path, encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def effective(env, dot):
    values = parse(read(env))
    values.update(parse(read(dot)))
    return values


def atomic_write(path, text):
    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


# --- configure -----------------------------------------------------------------

def configure(report):
    try:
        directory = config_dir()
        env, dot = files(directory)
    except Refused as error:
        report.fail(f"{error}; nothing written")
        return False
    overridden = sorted(set(MANAGED) & set(parse(read(dot))))
    if overridden:
        report.fail(f"{dot} sets {', '.join(overridden)}, which overrides the managed env; "
                    "remove those lines. Nothing written")
        return False
    text = read(env)
    lines = text.split("\n")
    exported = sorted({parsed[0] for parsed in map(parse_line, lines) if parsed
                       and parsed[0].startswith("export ") and parsed[0][7:].strip() in MANAGED})
    if exported:
        report.fail(f"{env} has '{', '.join(exported)}' lines; upstream ignores 'export'. "
                    "Remove the prefix. Nothing written")
        return False

    current = parse(text)
    token = current.get(TOKEN_KEY, "")
    if len(token) < MIN_TOKEN_LEN:
        if token:
            report.warn(f"{TOKEN_KEY} was shorter than {MIN_TOKEN_LEN} characters; replaced "
                        "(sign in again)")
        token = secrets.token_hex(32)
    if current.get(HOST_KEY, LOOPBACK) != LOOPBACK:
        report.warn(f"{HOST_KEY} was not {LOOPBACK}; rewritten")
    if current.get(UPDATE_KEY, "0") != "0":
        report.warn(f"{UPDATE_KEY} was not 0; rewritten")
    desired = {TOKEN_KEY: token, HOST_KEY: LOOPBACK, UPDATE_KEY: "0"}

    kept, seen = [], set()
    for line in lines:
        parsed = parse_line(line)
        if parsed and parsed[0] in MANAGED:
            if parsed[0] not in seen:
                seen.add(parsed[0])
                kept.append(f"{parsed[0]}={desired[parsed[0]]}")
            continue
        kept.append(line)
    if kept and kept[-1] == "":
        kept.pop()
    kept.extend(f"{key}={desired[key]}" for key in MANAGED if key not in seen)
    new_text = "\n".join(kept) + "\n"

    if new_text != text:
        atomic_write(env, new_text)
        report.ok(f"wrote {env}")
    else:
        report.ok(f"{env} already configured")
    directory.chmod(0o700)
    env.chmod(0o600)
    if dot.exists():
        dot.chmod(0o600)
    return True


# --- checks --------------------------------------------------------------------

def tailscale_gate(report):
    """Mirror upstream's owner lookup (User[Self.UserID].LoginName), whatever the
    backend state; ask every CLI found, since herdr's PATH may pick another."""
    raw = os.environ.get("HARNESS_HERDR_WEB_TAILSCALE_CANDIDATES") or ""
    candidates = raw.split(":") if raw else [shutil.which("tailscale"), TAILSCALE_APP]
    clis = [c for c in dict.fromkeys(candidates) if c and os.path.isfile(c) and os.access(c, os.X_OK)]
    if not clis:
        report.ok("Tailscale CLI not found")
        return True
    for cli in clis:
        if tailscale_login(cli):
            report.fail("Tailscale reports a login for this machine: unsupported with herdr web ui "
                        "for now (see docs/herdr-web-ui.md)")
            return False
    report.ok("no Tailscale login on this machine")
    return True


def tailscale_login(cli):
    try:
        result = run([cli, "status", "--json"], timeout=10)
        status = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if not isinstance(status, dict):
        return ""
    user_id = str((status.get("Self") or {}).get("UserID", "")) if isinstance(status.get("Self"), dict) else ""
    users = status.get("User") if isinstance(status.get("User"), dict) else {}
    user = users.get(user_id) if isinstance(users.get(user_id), dict) else {}
    return user.get("LoginName") or ""


def bun_version():
    try:
        result = run(["bun", "--version"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", result.stdout.strip())  # also "1.4.0-canary.1"
    return tuple(int(p) for p in match.groups()) if result.returncode == 0 and match else None


def bun_ok(report, fatal):
    version = bun_version()
    if version and version >= MIN_BUN:
        report.ok(f"bun {'.'.join(map(str, version))}")
        return True
    found = ".".join(map(str, version)) if version else "missing"
    message = (f"bun {found} is below {'.'.join(map(str, MIN_BUN))}: run 'bun upgrade' "
               "(herdr's own PATH must also reach bun)")
    (report.fail if fatal else report.warn)(message)
    return False


def plugin_root():
    try:
        result = run(["herdr", "plugin", "list", "--json"], timeout=15)
        plugins = json.loads(result.stdout)["result"]["plugins"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("plugin_id") == PLUGIN_ID:
            return plugin.get("plugin_root") or ""
    return None


def fetch(port, path, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{LOOPBACK}:{port}{path}", timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            return "refused", b""
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            return "timeout", b""
        return "error", b""
    except ConnectionRefusedError:
        return "refused", b""
    except (socket.timeout, TimeoutError):
        return "timeout", b""
    except (OSError, http.client.HTTPException):
        return "error", b""


def probe_timeout():
    try:
        return float(os.environ.get("HARNESS_HERDR_WEB_PROBE_TIMEOUT", "2"))
    except ValueError:
        return 2.0


def runtime_probe(report, port):
    status, body = fetch(port, "/api/health?scope=bridge", probe_timeout())
    if status == "refused":
        report.warn(f"herdr web ui is not running on {LOOPBACK}:{port}")
        return
    try:
        auth = json.loads(body).get("auth")
    except (ValueError, AttributeError):
        auth = None
    if not isinstance(auth, dict) or "authenticated" not in auth:
        report.fail(f"cannot verify the running server on {LOOPBACK}:{port} ({status})")
        return
    if auth["authenticated"]:
        report.fail("running server accepts unauthenticated loopback; restart: " + RESTART_HINT)
        return
    report.ok("running server rejects unauthenticated loopback")
    status, body = fetch(port, "/api/health", probe_timeout())
    try:
        revision = (json.loads(body).get("web_ui") or {}).get("revision") if status == 200 else ""
    except (ValueError, AttributeError):
        revision = ""
    if revision and revision != PINNED_COMMIT:
        report.warn(f"in-app release running ({revision[:12]}), not the pinned build")


def check(report):
    try:
        directory = config_dir()
        env, dot = files(directory)
    except Refused as error:
        report.fail(str(error))
        return
    if not env.exists():
        report.fail(f"{env} missing: run 'harness-herdr-web configure'")
        return
    for path, mode in ((directory, 0o700), (env, 0o600), (dot, 0o600)):
        actual = stat.S_IMODE(path.lstat().st_mode) if path.exists() else mode
        if actual != mode:
            report.fail(f"{path} mode is {actual:04o}, expected {mode:04o}: "
                        "run 'harness-herdr-web configure'")
    values = effective(env, dot)
    if len(values.get(TOKEN_KEY, "")) < MIN_TOKEN_LEN:
        report.fail(f"effective {TOKEN_KEY} is missing or shorter than {MIN_TOKEN_LEN} "
                    "characters (env, then .env)")
    else:
        report.ok(f"{TOKEN_KEY} set")
    if values.get(HOST_KEY) != LOOPBACK:
        # Unset falls back to HOST in herdr's own environment, which is unknowable here.
        report.fail(f"effective {HOST_KEY} is not {LOOPBACK} (a missing {HOST_KEY} falls back "
                    "to herdr's environment): run 'harness-herdr-web configure'")
    if values.get(UPDATE_KEY) == "1":
        report.fail(f"effective {UPDATE_KEY}=1 installs releases without review")
    tailscale_gate(report)
    bun_ok(report, fatal=False)

    root = plugin_root()
    if root is None:
        report.warn(f"{PLUGIN_ID} not installed")
        return
    head = ""
    try:
        result = run(["git", "-C", root, "rev-parse", "HEAD"], timeout=15)
        head = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        pass
    if head != PINNED_COMMIT:
        report.warn(f"unreviewed build: {root} is at {head[:12] or 'unknown'}, "
                    f"pinned {PINNED_REF} ({PINNED_COMMIT[:12]})")
    else:
        report.ok(f"plugin at pinned {PINNED_REF}")
    try:
        port = int(values.get("PORT", DEFAULT_PORT))
    except ValueError:
        report.fail("effective PORT is not a number")
        return
    runtime_probe(report, port)


# --- install / token -----------------------------------------------------------

def install(report, ref):
    if not tailscale_gate(report) or not bun_ok(report, fatal=True):
        report.fail("install refused before anything was installed")
        return
    if ref != PINNED_REF:
        report.warn(f"--ref {ref} is not the reviewed pin {PINNED_REF}")
    if not configure(report):
        return
    result = run(["herdr", "plugin", "install", PLUGIN_REPO, "--ref", ref, "--yes"], timeout=1800)
    if result.returncode != 0:
        report.fail(f"herdr plugin install failed ({result.returncode}); see "
                    f"'herdr plugin log {PLUGIN_ID}'")
        return
    report.ok(f"installed {PLUGIN_REPO} {ref}")
    try:
        server = json.loads(run(["herdr", "status", "server", "--json"], timeout=15).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        server = {}
    if not (isinstance(server, dict) and server.get("running") is True):
        report.warn(f"herdr server not running: start herdr, then run the start action "
                    f"(herdr plugin action invoke {PLUGIN_ID}.start)")
    elif tailscale_gate(report):
        run(["herdr", "plugin", "action", "invoke", f"{PLUGIN_ID}.start"], timeout=60)
        directory = config_dir()
        port = effective(directory / "env", directory / ".env").get("PORT", str(DEFAULT_PORT))
        if not port.isdigit():
            port = str(DEFAULT_PORT)
        deadline = time.monotonic() + float(os.environ.get("HARNESS_HERDR_WEB_START_TIMEOUT", "25"))
        while time.monotonic() < deadline and fetch(port, "/api/health?scope=bridge", 1)[0] == "refused":
            time.sleep(0.5)
    else:
        return
    check(report)


def copy_token(report):
    try:
        env, dot = files(config_dir())
    except Refused as error:
        report.fail(str(error))
        return
    token = effective(env, dot).get(TOKEN_KEY, "")
    if len(token) < MIN_TOKEN_LEN:
        report.fail(f"no usable {TOKEN_KEY}: run 'harness-herdr-web configure'")
        return
    result = run(["pbcopy"], input=token, timeout=10)
    if result.returncode != 0:
        report.fail("pbcopy failed")
        return
    report.ok("token copied to the clipboard (Universal Clipboard may sync it)")


def build_parser():
    parser = argparse.ArgumentParser(prog="harness-herdr-web", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="enforce token, loopback HOST and manual updates")
    install_parser = sub.add_parser("install", help=f"configure, then install the pinned {PINNED_REF}")
    install_parser.add_argument("--ref", default=PINNED_REF)
    sub.add_parser("check", help="OK/WARN/FAIL report; exit 1 on FAIL")
    token_parser = sub.add_parser("token", help="copy the token for the first browser login")
    token_parser.add_argument("--copy", action="store_true", required=True)
    usage_parser = sub.add_parser("usage", help="one line of Claude and Codex plan usage for herdr's tab bar")
    usage_parser.add_argument("--watch", action="store_true", help="a live board for a herdr popup; q closes")
    return parser


def main(argv):
    if argv and argv[0] == "cli":
        import harness_herdr_cli
        return harness_herdr_cli.main(argv[1:])
    args = build_parser().parse_args(argv)
    if args.command == "usage":
        import harness_herdr_usage
        return harness_herdr_usage.main(["--watch"] if args.watch else [])
    report = Report()
    try:
        if args.command == "configure":
            configure(report)
        elif args.command == "install":
            install(report, args.ref)
        elif args.command == "check":
            check(report)
        elif args.command == "token":
            copy_token(report)
    except Refused as error:
        report.fail(str(error))
    except (OSError, subprocess.SubprocessError) as error:
        report.fail(f"{type(error).__name__}: {error}")
    except Exception as error:  # never a traceback, and never the message (it could quote config)
        report.fail(f"unexpected {type(error).__name__}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
