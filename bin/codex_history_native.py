"""Offline Native full-history proof. Never sends a model turn or tool response."""
import hashlib
import base64
import json
import os
import platform
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess
import tempfile
import time

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
CHARACTERIZED_VERSIONS = {b"0.161.0", b"0.162.1"}


class NativeVerificationError(Exception):
    pass


def fail(message):
    raise NativeVerificationError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def expected_history(home):
    """Read the preserved projection before Native can migrate derived copies."""
    home = Path(home)
    headers = {}; content = {}; parents = {}; owners = set(); primary_paths={}
    for directory in ("sessions", "archived_sessions"):
        for path in sorted((home / directory).rglob("*.jsonl")):
            if path.is_symlink(): fail("unsafe Native rollout")
            thread = None
            # Segments without a header belong to the UUID encoded in their name.
            matches = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", path.name)
            if matches: thread = matches[-1].lower()
            filename_thread = thread
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip(): continue
                    row = json.loads(line)
                    if row.get("type") == "session_meta":
                        meta = row.get("payload", {}); thread = str(meta.get("id", "")).lower()
                        if not UUID.fullmatch(thread): fail("invalid Native thread metadata")
                        if filename_thread and filename_thread != thread: fail("Native filename and metadata disagree")
                        previous = headers.get(thread)
                        if previous and previous != meta: fail("conflicting Native thread metadata")
                        headers[thread] = meta
                        primary_paths[thread]=path
                        parent = (meta.get("history_base") or {}).get("thread_id")
                        if parent: parents[thread] = parent.lower()
                    if thread and row.get("type") == "response_item": content[thread] = True
                    if row.get('type') != 'session_meta':
                        if not thread: fail('Native rollout has no attributable UUID')
                        owners.add(thread)
    if not headers: fail("Native history has no readable thread metadata")
    if not owners <= set(headers): fail('Native rollout segment has no preserved thread metadata')
    if any(parent not in headers for parent in parents.values()): fail("Native parent history is missing")
    for thread in parents:
        seen = set(); current = thread
        while current in parents:
            if current in seen: fail("cyclic Native parent history")
            seen.add(current); current = parents[current]
    # Older rollout-only readers need independent characterization. Fail closed.
    if any(meta.get("history_mode") != "paginated" for meta in headers.values()):
        fail("Native history format is not characterized")
    database = home / "thread_history_1.sqlite"
    if not database.is_file() or database.is_symlink(): fail("Native history projection is missing")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'thread_history_projection_state' in tables:
            for thread,offset in connection.execute('SELECT thread_id,next_rollout_byte_offset FROM thread_history_projection_state'):
                if thread not in primary_paths or offset<0 or offset>primary_paths[thread].stat().st_size:
                    fail('Native projection cursor is outside preserved rollout')
        stored={}
        for thread in sorted(headers):
            turns = list(connection.execute(
                "SELECT turn_id,rollout_ordinal FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal,turn_id", (thread,)))
            items = list(connection.execute(
                "SELECT turn_id,item_json,rollout_ordinal FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal,item_id", (thread,)))
            stored[thread]={'turns':turns,'items':items}
        projected=set(row[0] for row in connection.execute('SELECT DISTINCT thread_id FROM thread_items UNION SELECT DISTINCT thread_id FROM thread_turns'))
        if not projected<=set(headers):fail('Native projection has no preserved rollout metadata')
        def projection(thread,cut=None):
            inherited={'turns':[],'items':[]}
            if thread in parents:
                base=headers[thread]['history_base'];parent=parents[thread]
                ordinal=base.get('end_ordinal_exclusive');offset=base.get('end_byte_offset')
                if not isinstance(ordinal,int) or ordinal<0 or not isinstance(offset,int) or offset<0:
                    fail('Native parent cut is not characterized')
                path=primary_paths[parent];boundaries={0:0};position=0
                with path.open('rb') as stream:
                    for line in stream:
                        position+=len(line);row=json.loads(line);index=row.get('ordinal')
                        if not isinstance(index,int):fail('Native parent ordinal layout is unsupported')
                        boundaries[position]=index+1
                if boundaries.get(offset)!=ordinal:fail('Native parent cut is outside preserved history')
                inherited=projection(parent,ordinal)
            own=stored[thread]
            return {key:inherited[key]+[row for row in own[key] if cut is None or row[-1]<cut] for key in ('turns','items')}
        result={}
        for thread in sorted(headers):
            rows=projection(thread)
            turns=[row[0] for row in rows['turns']]
            items=[{'turnId':row[0],'item':json.loads(row[1])} for row in rows['items']]
            if content.get(thread) and (not turns or not items): fail("Native projection omits transcript content")
            if any(row["turnId"] not in turns for row in items): fail("Native item has no preserved turn")
            result[thread] = {"turns": turns, "items": items}
        return result
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise NativeVerificationError("Native projection is unsupported or invalid") from error
    finally:
        connection.close()


def sandbox_command(binary, arguments, denied=(), home=None):
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file(): fail("offline Native sandbox is unavailable")
    profile = '(version 1)(allow default)(deny network*)'
    profile += '(deny file-write*)'
    if home:profile += '(allow file-write* (subpath '+json.dumps(str(Path(home).resolve()))+'))'
    profile += '(allow file-write-data (literal "/dev/null"))'
    # No other catalog or Native home can supply missing restored artifacts.
    profile += '(deny file-read-data)'
    readable=['/System','/usr','/dev','/private/etc']
    if home: readable.append(str(Path(home).resolve()))
    for path in readable:
        profile += '(allow file-read-data (subpath '+json.dumps(path)+'))'
    profile += '(allow file-read-data (literal '+json.dumps(str(Path(binary).resolve()))+'))'
    # dyld also reads ancestor directories. Allow listing those exact dirs,
    # without admitting any sibling transcript or catalog file contents.
    ancestors=set(Path(binary).resolve().parents)
    if home:ancestors.update(Path(home).resolve().parents)
    for path in sorted(ancestors,key=str):
        profile += '(allow file-read-data (literal '+json.dumps(str(path))+'))'
    for path in denied:
        profile += '(deny file-read* (subpath ' + json.dumps(str(Path(path).absolute())) + '))'
    return [str(sandbox), "-p", profile, str(binary), *arguments]


def private_env(home):
    # Credentials, proxies, selected config and parent-process model settings are
    # deliberately absent. Node-backed distributions still need a system PATH.
    return {"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "HOME": str(home),
            "CODEX_HOME": str(home), "LANG": "en_US.UTF-8"}


def file_digest(path):
    checksum=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024),b''):checksum.update(chunk)
    return checksum.hexdigest()


def binary_identity(binary):
    wrapper = Path(binary).resolve(strict=True);path=wrapper
    if wrapper.name=='codex.js' and wrapper.parent.name=='bin' and wrapper.parent.parent.name=='codex':
        target={'arm64':'aarch64-apple-darwin','x86_64':'x86_64-apple-darwin'}.get(platform.machine())
        package={'arm64':'codex-darwin-arm64','x86_64':'codex-darwin-x64'}.get(platform.machine())
        if not target:fail('Native package architecture is not characterized')
        root=wrapper.parent.parent
        candidates=[root/'node_modules/@openai'/package/'vendor'/target/'bin/codex',
                    root.parent/package/'vendor'/target/'bin/codex',root/'vendor'/target/'bin/codex']
        found=[p.resolve() for p in candidates if p.is_file()]
        if len(set(found))!=1:fail('Native package backend is missing or ambiguous')
        path=found[0]
    if not path.is_file() or not os.access(path, os.X_OK): fail("Native executable unavailable")
    with tempfile.TemporaryDirectory(prefix="native-version-") as temp:
        process = subprocess.run(sandbox_command(path, ["--version"],home=temp), env=private_env(temp),
                                 capture_output=True, timeout=10, check=False)
    version = re.search(rb"codex-cli (\d+\.\d+\.\d+)", process.stdout)
    if process.returncode or not version: fail("Native version unavailable")
    # The paginated wire protocol was verified against these versions; future
    # versions require characterization rather than implied compatibility.
    if version.group(1) not in CHARACTERIZED_VERSIONS: fail("Native version lacks characterized history verification")
    return {"version": version.group(1).decode(), "sha256": file_digest(path),
            'executable':str(path),'wrapper':str(wrapper),'wrapper_sha256':file_digest(wrapper)}


class NativeRpc:
    def __init__(self, binary, home, denied):
        self.binary = binary; self.home = Path(home); self.denied = denied
        self.buffer = b""; self.sequence = 0; self.process = None

    def __enter__(self):
        self.process = subprocess.Popen(sandbox_command(self.binary, ["app-server", "--listen", "stdio://"], self.denied,home=self.home),
            env=private_env(self.home), cwd=self.home, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector(); self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.call("initialize", {"clientInfo": {"name": "harness_history_verifier", "version": "1"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.__exit__(None, None, None); raise
        return self

    def __exit__(self, *unused):
        if self.process is not None:
            self.process.terminate()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=3)
            self.process.stdin.close(); self.process.stdout.close(); self.selector.close()

    def send(self, value):
        self.process.stdin.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1; request = self.sequence
        self.send({"id": request, "method": method, "params": params})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            while b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.selector.select(remaining): fail("Native history RPC timed out")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk: fail("Native history RPC closed unexpectedly")
                self.buffer += chunk
                if len(self.buffer) > 64 * 1024 * 1024: fail("Native history RPC exceeded response limit")
            line, self.buffer = self.buffer.split(b"\n", 1)
            try: response = json.loads(line)
            except json.JSONDecodeError: fail("Native history RPC returned invalid JSON")
            if "method" in response and "id" in response:
                fail("Native history probe requested tool execution or approval")
            if response.get("id") != request: continue
            if "error" in response:
                self.last_error=response['error']
                fail("Native rejected the full-history probe")
            result = response.get("result")
            if not isinstance(result, dict): fail("Native history RPC result is invalid")
            return result
        fail("Native history RPC timed out")


def pages(rpc, method, thread):
    rows = []; cursor = None; seen = set()
    for unused in range(10000):
        params = {"threadId": thread, "limit": 100, "sortDirection": "asc"}
        if cursor: params["cursor"] = cursor
        result = rpc.call(method, params)
        if not isinstance(result.get("data"), list): fail("Native history page is invalid")
        rows.extend(result["data"]); cursor = result.get("nextCursor")
        if not cursor: return rows
        if cursor in seen: fail("Native history cursor repeated")
        seen.add(cursor)
    fail("Native history page limit exceeded")


def verify_native(restored_home, manifest, codex_bin):
    expected = expected_history(restored_home)
    identity = binary_identity(codex_bin)
    executable=identity.get('executable',str(Path(codex_bin).resolve()))
    if manifest.get('source'):
        original=Path(manifest['source']);ephemeral=str(original.parent.parent)
        def check_paths(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in ('path','image_path','file_path','local_path','artifact_path','image_url') and isinstance(item,str):
                        path=item.removeprefix('file://')
                        if path==ephemeral or path.startswith(ephemeral+'/'):
                            fail('Native attachment refers to the retiring workspace')
                    check_paths(item)
            elif isinstance(value,list):
                for item in value:check_paths(item)
        for projection in expected.values():check_paths(projection['items'])
    denied = [manifest[key] for key in ("source", "retained_source") if manifest.get(key)]
    receipts = []; files = []
    # Native itself reads every raw JSONL byte, including segments and parents.
    # The wire projection and the model-visible rollout are separate artifacts.
    for directory in ('sessions','archived_sessions'):
        for path in sorted((Path(restored_home)/directory).rglob('*.jsonl')):
            files.append((path,hashlib.sha256(path.read_bytes()).hexdigest()))
    with NativeRpc(executable, restored_home, denied) as rpc:
        for path,checksum in files:
            result=rpc.call('fs/readFile',{'path':str(path)})
            try: contents=base64.b64decode(result['dataBase64'],validate=True)
            except (KeyError,ValueError,TypeError): fail('Native raw history read is invalid')
            if hashlib.sha256(contents).hexdigest()!=checksum: fail('Native raw history differs from restored archive')
        for thread, projection in expected.items():
            result = rpc.call("thread/resume", {"threadId": thread, "cwd": str(restored_home),
                              "excludeTurns": True, "sandbox": "read-only", "approvalPolicy": "untrusted"})
            if result.get("thread", {}).get("id") != thread: fail("Native resumed a different thread")
            turns = pages(rpc, "thread/turns/list", thread)
            items = pages(rpc, "thread/items/list", thread)
            turn_ids = [row.get("id") for row in turns]
            actual = [{"turnId": row.get("turnId"), "item": row.get("item")} for row in items]
            if turn_ids != projection["turns"] or actual != projection["items"]:
                fail("Native full history differs from preserved projection")
            receipts.append({"native_id": thread, "turn_count": len(turns), "item_count": len(items),
                             "history_sha256": digest(actual)})
    if identity.get('executable') and binary_identity(codex_bin)!=identity:
        fail('Native executable changed during verification')
    return {"schema_version": 1, "native": identity, "threads": receipts,
            'raw_rollout_count':len(files),
            "model_requests": 0, "network": "sandbox-denied"}
