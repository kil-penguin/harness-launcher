#!/usr/bin/env python3
"""Private, on-demand storage for durable native Codex history."""

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import io
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import functools


FILE_NAMES = {"history.jsonl", "session_index.jsonl", "external_agent_session_imports.json"}
DIRECTORIES = {"sessions", "archived_sessions", "logs_2", "goals_1", "memories_1",
               "queue_1", "shell_snapshots", "generated_images", "attachments", "memories", "log"}
EXCLUDED = {"auth.json", "config.toml", "hooks.json", "skills", "plugins",
            ".codex-home-prepare.lock", ".surface-success.json", ".surface-fingerprint-cache.json",
            "cache", "installation_id", ".personality_migration", ".sandbox_migration",
            ".sqlite-maintenance.lock", "thread-writer-locks", "mcp-oauth-locks",
            ".surface-quarantine", "tmp", ".tmp", "models_cache.json", "version.json",
            "AGENTS.md", "agents", "skill-catalog.json"}
# Native app, daemon and tool state. Never history; it changes while the home is
# otherwise stable, so the inventory records only its name and kind.
RUNTIME_STATE = {"app-server-control", "app-server-daemon", "process_manager", "ambient-suggestions",
                 "vendor_imports", "computer-use", ".codex-global-state.json", ".codex-global-state.json.bak",
                 ".cmux-title-sync", ".glider", ".DS_Store"}
# Generic names count as runtime state only while they hold exactly the known
# content: the app inbox/automation database, or nothing.
RUNTIME_STATE_CONTENT = {"sqlite": re.compile(r"codex-dev\.db(?:-wal|-shm|-journal)?"), "tui-luna-reserve": None}
AUTHORITY = {"rules"}  # Native execpolicy command approvals.
# Interrupted mktemp siblings of generated files (codex-home-prepare.sh). A
# six-letter word suffix looks the same; every prefix names generated config.
GENERATED_TEMP = re.compile(r"\.(?:AGENTS\.md|AGENTS\.prev|config\.toml|hooks\.json|[A-Za-z0-9_-]+\.config\.toml"
                            r"|plugin-[A-Za-z0-9_.-]+)\.[A-Za-z0-9]{6}")
SQLITE_NAME = re.compile(r"(?:state|thread_history|history|logs|goals|memories|queue)_\d+\.sqlite$")
KNOWN_SQLITE = {'state_5.sqlite','thread_history_1.sqlite','logs_2.sqlite','goals_1.sqlite','memories_1.sqlite','queue_1.sqlite'}
REVISION = re.compile(r"[0-9a-f]{40}|legacy-unknown$")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


class Refusal(Exception):
    code = 2


class ActiveWriter(Refusal):
    code = 3


def refuse(message):
    raise Refusal(message)


def mode(path, value):
    os.chmod(path, value)


def regular(path):
    return path.is_file() and not path.is_symlink() and path.stat().st_uid == os.getuid() and path.stat().st_nlink == 1


def bound_path(path, anchor):
    """Reject redirected components inside a trusted source or state boundary."""
    path,anchor=Path(path).absolute(),Path(anchor).absolute()
    try:parts=path.relative_to(anchor).parts
    except ValueError:refuse('path crosses trusted boundary')
    current=anchor
    for part in (None,*parts):
        if part is not None:current=current/part
        try:metadata=current.lstat()
        except FileNotFoundError:return
        except OSError:refuse('unreadable path boundary')
        if stat.S_ISLNK(metadata.st_mode):refuse('symlinked path boundary')
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid():refuse('unsafe path boundary')
        try:
            with os.scandir(current):pass
        except OSError:refuse('unreadable path boundary')


def native_presence(path, anchor):
    """Absence is trusted only after every existing directory is accessible."""
    path = Path(path)
    bound_path(path, anchor)
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        refuse('unreadable Native presence boundary')
    return True


def presence(args):
    print('native' if native_presence(Path(args.source), Path(args.anchor)) else 'absent')


def secure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        refuse("unsafe directory")
    mode(path, 0o700)


def write_private_json(path, value):
    if path.exists() or path.is_symlink():
        refuse("receipt already exists")
    secure_dir(path.parent)
    temp = path.parent / (path.name + ".tmp-" + uuid.uuid4().hex)
    descriptor=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"))+'\n')
        stream.flush();os.fsync(stream.fileno())
    os.replace(temp, path)
    descriptor=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root):
    bound_path(root,root)
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            refuse("symlinked native artifact")
        if path.is_dir():
            bound_path(path,root)
            continue
        if not regular(path):
            refuse("non-regular native artifact")
        entries.append({"path": relative, "sha256": sha256(path), "size": path.stat().st_size})
    return entries


def runtime_state(path):
    if path.name in RUNTIME_STATE:
        return True
    if path.name not in RUNTIME_STATE_CONTENT or path.is_symlink() or not path.is_dir():
        return False
    allowed = RUNTIME_STATE_CONTENT[path.name]
    try:
        return all(allowed is not None and allowed.fullmatch(child.name) and child.is_file() and not child.is_symlink()
                   for child in path.iterdir())
    except OSError:
        refuse("unreadable runtime state")


def excluded(path):
    # Excluded entries are never archived, so retiring a root discards them.
    name = path.name
    return (name in EXCLUDED or name in AUTHORITY or name.endswith(".config.toml")
            or GENERATED_TEMP.fullmatch(name) is not None or runtime_state(path))


def source_children(source):
    if source.is_symlink() or not source.is_dir():
        refuse("source home is unsafe")
    children = []
    for path in source.iterdir():
        name = path.name
        if excluded(path):
            continue
        if sqlite_sidecar(name):
            if name[:-4] not in KNOWN_SQLITE or not regular(source/name[:-4]):
                refuse('orphan or unknown Native SQLite sidecar')
            continue
        if path.is_symlink():
            refuse("symlinked top-level artifact")
        if name in DIRECTORIES and path.is_dir():
            children.append(path)
        elif name in FILE_NAMES and regular(path):
            children.append(path)
        elif SQLITE_NAME.fullmatch(name) and regular(path):
            if name not in KNOWN_SQLITE:refuse('unknown Native SQLite version')
            children.append(path)
        else:
            refuse("unknown top-level native artifact")
    return children


def parse_jsonl(path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                try:
                    json.loads(line)
                except json.JSONDecodeError as error:
                    raise Refusal("malformed native JSONL") from error


def validate_jsonl(children):
    for child in children:
        paths = [child] if child.name.endswith(".jsonl") else child.rglob("*.jsonl")
        for path in paths:
            if path.is_symlink():
                refuse("symlinked native JSONL")
            parse_jsonl(path)


def sqlite_paths(children):
    return [path for path in children if SQLITE_NAME.fullmatch(path.name)]


def sqlite_sidecar(name):
    base = name.removesuffix("-wal").removesuffix("-shm")
    return base != name and SQLITE_NAME.fullmatch(base) is not None


def source_inventory(source, children):
    result = inventory_children(source, children)
    for path in sorted(source.iterdir()):
        if not excluded(path):
            continue
        meta = path.lstat()
        kind = "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file"
        row = {"path": path.name, "excluded": True, "kind": kind}
        if not runtime_state(path):
            row.update(size=meta.st_size, mtime_ns=meta.st_mtime_ns)
        result.append(row)
    return sorted(result, key=lambda row: row["path"])


def inventory_children(source, children):
    result = []
    for child in children:
        if child.is_dir():bound_path(child,source)
        paths = [child] if child.is_file() else sorted(child.rglob("*"))
        for path in paths:
            if path.is_symlink():
                refuse("unsafe native artifact")
            if path.is_dir():
                bound_path(path,source)
                continue
            if not regular(path):
                refuse("unsafe native artifact")
            result.append({"path": path.relative_to(source).as_posix(), "sha256": sha256(path),
                           "size": path.stat().st_size})
    for path in sorted(source.iterdir()):
        if sqlite_sidecar(path.name):
            if path.is_symlink() or not regular(path):
                refuse("unsafe SQLite sidecar")
            result.append({"path": path.name, "sha256": sha256(path), "size": path.stat().st_size})
    return sorted(result, key=lambda row: row["path"])


def sqlite_ready(path):
    writer_lock_probe(path)


def lock_byte(path, offset):
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as error:
        raise ActiveWriter("native SQLite lock probe unavailable") from error
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, offset, os.SEEK_SET)
        fcntl.lockf(descriptor, fcntl.LOCK_UN, 1, offset, os.SEEK_SET)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise ActiveWriter("native SQLite has an active writer") from error
        raise ActiveWriter("native SQLite lock probe unavailable") from error
    finally:
        os.close(descriptor)


def writer_lock_probe(path):
    lock_byte(path, 0x40000001)
    shared = Path(str(path) + "-shm")
    if shared.exists():
        if shared.is_symlink() or not regular(shared):
            refuse("unsafe SQLite shared-memory file")
        lock_byte(shared, 120)


def copy_sqlite(source, destination):
    sqlite_ready(source)
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".sqlite-copy-"))
        copied = temporary / source.name
        shutil.copyfile(source, copied)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, Path(str(copied) + suffix))
        read = sqlite3.connect(copied)
        if read.execute("PRAGMA quick_check").fetchone() != ("ok",):
            refuse("invalid native SQLite")
        tables={row[0] for row in read.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'remote_control_enrollments' in tables and read.execute('SELECT 1 FROM remote_control_enrollments LIMIT 1').fetchone():
            refuse('Native database contains remote-control authority')
        write = sqlite3.connect(destination)
        read.backup(write)
        write.close()
        read.close()
    except sqlite3.Error as error:
        raise Refusal("SQLite backup failed") from error
    finally:
        if "temporary" in locals():
            shutil.rmtree(temporary)
    mode(destination, 0o600)


def copy_regular(source, destination):
    metadata=source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode(destination.parent, 0o700)
    shutil.copyfile(source, destination)
    os.utime(destination,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    mode(destination, 0o600)


def sync_tree(root):
    for path in sorted(root.rglob('*'),reverse=True):
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    descriptor=os.open(root,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def archive_children(source, children, data):
    databases = set(sqlite_paths(children))
    for child in children:
        targets = [child] if child.is_file() else sorted(child.rglob("*"))
        for path in targets:
            if path.is_symlink():
                refuse("unsafe native artifact")
            if path.is_dir():
                continue
            if not regular(path):
                refuse("unsafe native artifact")
            relative = path.relative_to(source)
            target = data / relative
            if child in databases and path == child:
                copy_sqlite(path, target)
            else:
                copy_regular(path, target)


def manifest_id(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def imported_native_tuples(items, owner):
    if not isinstance(items, list) or len(items) > 10000:
        refuse('invalid imported Native ledger')
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'runtime', 'native_id', 'source_owner'}
                or item['runtime'] != 'codex' or not isinstance(item['native_id'],str)
                or not UUID.fullmatch(item['native_id']) or not isinstance(item['source_owner'],str)
                or not UUID.fullmatch(item['source_owner'])
                or item['source_owner'].lower() == owner.lower()
                or item['native_id'].lower() in seen):
            refuse('invalid imported Native ledger')
        seen.add(item['native_id'].lower())
    return seen


def native_import_provenance(home, source, owner):
    if owner == 'canonical':return None
    home = Path(home)
    state = home.parent.parent.parent.parent
    if home != state/'worktrees'/owner/'.harness/codex':return None
    record = state/'sessions'/owner
    bound_path(record, state)
    path = record/'restored-native-sessions'
    try:metadata = path.lstat()
    except FileNotFoundError:return None
    except OSError:refuse('invalid imported Native ledger')
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
        refuse('invalid imported Native ledger')
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:raise ValueError('duplicate ledger key')
            value[key] = item
        return value
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            stamp = lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,
                                info.st_uid,info.st_nlink,info.st_mode)
            if before.st_size > 65536 or stamp(before) != stamp(metadata):raise ValueError('ledger changed')
            raw = stream.read(65537)
            if len(raw) > 65536 or stamp(os.fstat(stream.fileno())) != stamp(before) or stamp(path.lstat()) != stamp(before):
                raise ValueError('ledger changed')
        value = json.loads(raw, object_pairs_hook=unique_keys)
        if (not isinstance(value,dict) or set(value) != {'schema','session_id','source_root','imports'}
                or type(value['schema']) is not int or value['schema'] != 1
                or value['session_id'] != owner or value['source_root'] != str(Path(source).resolve())):
            raise ValueError('invalid ledger binding')
        imported_native_tuples(value['imports'], owner)
    except (OSError, ValueError, UnicodeError, TypeError):
        refuse('invalid imported Native ledger')
    return {'imports':value['imports'], 'ledger_sha256':hashlib.sha256(raw).hexdigest()}


def snapshot_import_ids(value):
    proof = value.get('native_import_provenance')
    if proof is None:return set()
    owner = value.get('isolation_id','')
    if (not UUID.fullmatch(str(owner)) or not isinstance(proof,dict)
            or set(proof) != {'imports','ledger_sha256'}
            or not re.fullmatch('[0-9a-f]{64}',str(proof['ledger_sha256']))):
        refuse('invalid imported Native ledger')
    return imported_native_tuples(proof['imports'],owner)


def validate_provenance(args):
    canonical = args.isolation_id == "canonical"
    if not canonical and not UUID.fullmatch(args.isolation_id):
        refuse("invalid isolation UUID")
    if not REVISION.fullmatch(args.runtime_revision):
        refuse("invalid runtime revision")
    root = Path(args.source_root).absolute()
    source = Path(args.source).absolute()
    if canonical and source != root / ".harness" / "codex":
        refuse("canonical provenance does not match canonical home")
    if root not in (source, *source.parents):
        state = Path(args.catalog).absolute().parent
        bound_path(source,state)
        record = state / "sessions" / args.isolation_id / "source-root"
        bound_path(record.parent,state)
        expected = state / "worktrees" / args.isolation_id / ".harness" / "codex"
        if source != expected or not regular(record):
            refuse("isolated source provenance is unverified")
        if record.read_text(encoding="utf-8").strip() != str(root):
            refuse("isolated source root mismatches provenance")
        inode = record.parent/'root-inode'
        try:valid_inode = regular(inode) and root_identity_matches(inode.read_text(), source.parent.parent)
        except (OSError, UnicodeError):valid_inode = False
        if not valid_inode:
            refuse('unverified Native root inode')
    else:bound_path(source,root)
    return source, root


def exclude_native_writers(operation):
    @functools.wraps(operation)
    def guarded(args):
        home=Path(args.source);descriptors=[]
        validate_provenance(args)
        if home.is_symlink() or not home.is_dir():refuse('source home is unsafe')
        try:
            files=[]
            for name in ('thread-writer-locks','mcp-oauth-locks'):
                directory=home/name
                if directory.is_symlink():refuse('unsafe Native writer lock directory')
                if directory.is_dir():files.extend(directory.iterdir())
            maintenance=home/'.sqlite-maintenance.lock'
            if maintenance.exists() or maintenance.is_symlink():files.append(maintenance)
            for path in sorted(files):
                if not regular(path):refuse('unsafe Native writer lock')
                try:
                    descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC);descriptors.append(descriptor)
                    fcntl.lockf(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except OSError as error:raise ActiveWriter('Native home has an active writer or unavailable lock') from error
            return operation(args)
        finally:
            for descriptor in descriptors:os.close(descriptor)
    return guarded


@exclude_native_writers
def snapshot(args):
    source, root = validate_provenance(args)
    catalog = Path(args.catalog)
    receipt = Path(args.receipt)
    imports = native_import_provenance(source, root, args.isolation_id)
    if catalog.exists() and (catalog.is_symlink() or not catalog.is_dir()):
        refuse("unsafe catalog")
    children = source_children(source)
    before = source_inventory(source, children)
    validate_jsonl(children)
    for database in sqlite_paths(children):
        sqlite_ready(database)
    secure_dir(catalog)
    snapshots = catalog / "snapshots"
    secure_dir(snapshots)
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=snapshots))
    try:
        data = staging / "data"
        secure_dir(data)
        archive_children(source, children, data)
        after = source_inventory(source, children)
        if after != before:
            refuse("source changed during snapshot")
        if native_import_provenance(source, root, args.isolation_id) != imports:
            refuse('imported Native ledger changed during snapshot')
        archive = inventory(data)
        revision=args.runtime_revision
        if args.isolation_id != 'canonical' and source == catalog.parent/'worktrees'/args.isolation_id/'.harness/codex':
            revision=binding_origin(catalog.parent,args.isolation_id,root)
        base = {"schema_version": 1, "source": str(source), "source_root": str(root),
                "isolation_id": args.isolation_id, "runtime_revision": revision,
                "source_inventory": before, "archive_inventory": archive}
        if imports is not None:base['native_import_provenance'] = imports
        if source == catalog.parent/'worktrees'/args.isolation_id/'.harness/codex':
            lineage=catalog.parent/'sessions'/args.isolation_id/'native-history-restore.json'
            if lineage.exists():
                if not regular(lineage):refuse('unsafe snapshot lineage')
                value=json.loads(lineage.read_text(encoding='utf-8'))
                if not UUID.fullmatch(str(value.get('native_id',''))):refuse('snapshot lineage has invalid native UUID')
                _,parent=load_snapshot(catalog,value.get('snapshot_id',''))
                if parent['source_root']!=str(root):refuse('snapshot lineage crosses source boundary')
                base.update(restored_native_id=value['native_id'],origin_snapshot=value['snapshot_id'],
                            origin_runtime=value.get('origin_runtime') or parent['runtime_revision'])
        snapshot_id = manifest_id(base)
        target = snapshots / snapshot_id
        if target.exists():
            load_snapshot(catalog,snapshot_id)
            if inventory(target/'data')!=archive:refuse('existing archived content differs')
            shutil.rmtree(staging)
        else:
            (staging / "manifest.json").write_text(json.dumps({**base, "snapshot_id": snapshot_id}, sort_keys=True))
            mode(staging / "manifest.json", 0o600)
            mode(staging, 0o700)
            sync_tree(staging)
            os.replace(staging, target)
            descriptor=os.open(snapshots,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        result = {"snapshot_id": snapshot_id, "source_sha256": manifest_id(before),
                  "source_home": str(source), "source_root": str(root),
                  "isolation_id": args.isolation_id, "runtime_revision": revision}
        write_private_json(receipt, result)
        print(json.dumps(result, sort_keys=True))
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_snapshot(catalog, snapshot_id):
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        refuse("invalid snapshot id")
    root = Path(catalog) / "snapshots" / snapshot_id
    bound_path(root/'data',Path(catalog))
    manifest = root / "manifest.json"
    if root.is_symlink() or not regular(manifest):
        refuse("missing snapshot")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise Refusal("invalid snapshot manifest") from error
    if value.get("snapshot_id") != snapshot_id:
        refuse("snapshot identity mismatch")
    if manifest_id({key:item for key,item in value.items() if key!='snapshot_id'}) != snapshot_id:
        refuse('snapshot manifest hash mismatch')
    snapshot_import_ids(value)
    return root, value


def relocate_state(database, source_home, destination):
    connection = sqlite3.connect(database)
    try:
        for table in ("threads", "rollout_migration_skipped_rollouts"):
            columns = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
            if "rollout_path" in columns:
                connection.execute("UPDATE " + table + " SET rollout_path = ? || substr(rollout_path, ?) "
                                   "WHERE substr(rollout_path,1,?)=?",
                                   (str(destination),len(source_home)+1,len(source_home)+1,source_home+'/'))
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            refuse("restored state SQLite invalid")
    finally:
        connection.close()
    mode(database, 0o600)


def restore(args):
    root, manifest = load_snapshot(args.catalog, args.snapshot)
    destination = Path(args.destination)
    if destination.exists():
        for path in destination.iterdir():
            if not excluded(path):
                refuse("restore destination has native or unknown data")
    secure_dir(destination)
    data = root / "data"
    if inventory(data) != manifest.get("archive_inventory"):
        refuse("archived content hash mismatch")
    for source in sorted(data.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(data)
        target = destination / relative
        if target.exists() or target.is_symlink():
            refuse("restore would overwrite native data")
        copy_regular(source, target)
    state = destination / "state_5.sqlite"
    if state.exists():
        relocate_state(state, manifest["source"], destination)
    result = {"snapshot_id": args.snapshot, "destination_sha256": manifest_id(inventory_children(destination,source_children(destination)))}
    if args.native_id:
        if not UUID.fullmatch(args.native_id):
            refuse("invalid native UUID")
        result["native_id"] = args.native_id
    if args.origin_runtime:
        if not REVISION.fullmatch(args.origin_runtime):
            refuse("invalid origin runtime")
        result["origin_runtime"] = args.origin_runtime
    write_private_json(Path(args.receipt), result)
    print(json.dumps(result, sort_keys=True))


def verify(args):
    root, manifest = load_snapshot(args.catalog, args.snapshot)
    def check_imports():
        if native_import_provenance(Path(manifest['source']),Path(manifest['source_root']),manifest['isolation_id']) != manifest.get('native_import_provenance'):
            refuse('imported Native ledger changed before retirement')
    check_imports()
    if inventory(root / "data") != manifest.get("archive_inventory"):
        refuse("archived content hash mismatch")
    source = Path(args.source).absolute()
    if source.is_symlink() or not source.is_dir():
        refuse("retained native source is unsafe")
    source_children(source)
    if source_inventory(source, source_children(source)) != manifest.get("source_inventory"):
        refuse("retained native source hash mismatch")
    original = Path(manifest["source"]).absolute()
    if original.exists() or original.is_symlink():
        refuse("original native source path remains visible")
    binary = Path(args.codex_bin)
    if not binary.exists() or not os.access(binary.resolve(), os.X_OK):
        refuse("native Codex binary unavailable")
    with tempfile.TemporaryDirectory(prefix="codex-history-verify-") as temporary:
        restored = Path(temporary) / "codex"
        restore_archive(root, manifest, restored)
        native_manifest = dict(manifest)
        native_manifest["retained_source"] = str(source)
        try:
            module_dir = str(Path(__file__).resolve().parent)
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)
            from codex_history_native import NativeVerificationError, verify_native
        except (ImportError, OSError) as error:
            raise Refusal("native history probe unavailable") from error
        try:
            proof = verify_native(restored, native_manifest, str(binary))
        except NativeVerificationError as error:
            raise Refusal("native history probe failed") from error
    if source_inventory(source, source_children(source)) != manifest.get("source_inventory"):
        refuse("retained native source changed during verification")
    check_imports()
    result = {"schema_version": 1, "snapshot_id": args.snapshot, "safe_to_retire": True, "proof": proof}
    receipt = root / 'verifications' / (manifest_id(result)+'.json')
    if receipt.exists():
        if not regular(receipt) or json.loads(receipt.read_text(encoding="utf-8")) != result:
            refuse("verification receipt conflicts")
    else:
        write_private_json(receipt, result)
    print(json.dumps({"snapshot_id": args.snapshot, "safe_to_retire": True}, sort_keys=True))


def restore_archive(root, manifest, destination):
    secure_dir(destination)
    data = root / "data"
    if inventory(data) != manifest.get("archive_inventory"):
        refuse("archived content hash mismatch")
    for source in sorted(data.rglob("*")):
        if source.is_file():
            copy_regular(source, destination / source.relative_to(data))
    state = destination / "state_5.sqlite"
    if state.exists():
        relocate_state(state, manifest["source"], destination)


def catalog(args):
    print(json.dumps(catalog_value(args),sort_keys=True))


def catalog_value(args, keep_private=False):
    source = Path(args.source_root).absolute()
    current=Path(args.state_home)
    states=[current]
    legacy=getattr(args,'legacy_state_home',None)
    if legacy and Path(legacy)!=current:states.append(Path(legacy))
    entries = [];warnings=[];held_ids=set();incomplete_hints=False
    for state in states:
        quarantines=[]
        homes=isolated_homes(state,source,quarantines)
        for home,isolation,reason in quarantines:
            hints,complete=quarantined_filename_hints(home,state)
            held_ids.update(hints);incomplete_hints=incomplete_hints or not complete
            warnings.append({'pool':'current' if state==current else 'legacy',
                             'isolation_id':isolation,'reason':reason})
        if state==current:homes.insert(0,(source/'.harness/codex','canonical','legacy-unknown','canonical'))
        for home,isolation,origin,status in homes:
            rows=home_entries(home,source,isolation,origin,status,None)
            for row in rows:row.update(_store=str(state/'native-history'),_state_home=str(state))
            entries.extend(rows)
        entries.extend(snapshot_entries(state/'native-history',source))
    # Only the requested id is deduped; held_ids and incomplete_hints above were
    # already collected from every quarantined home, so the verdict is unchanged.
    native_id=getattr(args,'native_id',None)
    if native_id:
        if not UUID.fullmatch(native_id):refuse("invalid selected native UUID")
        entries=[row for row in entries if row['native_id'].lower()==native_id.lower()]
    rows=dedupe_entries(entries,current/'native-history',keep_private)
    for row in rows:
        if incomplete_hints or row['native_id'].lower() in held_ids:
            row.update(status='ambiguous',reason='unverified_native_owner_hints_incomplete' if incomplete_hints else 'unverified_native_owner')
    return {'schema_version':1,'entries':rows,'warnings':warnings}


def binding_origin(state, identifier, source):
    binding=state.parent/'bindings'/(identifier.lower()+'.json')
    if not binding.exists():return 'legacy-unknown'
    if not regular(binding) or stat.S_IMODE(binding.stat().st_mode)!=0o600:refuse('unsafe runtime binding')
    value=json.loads(binding.read_text(encoding='utf-8'))
    pin=value.get('runtime',{});rev=pin.get('revision','')
    if value.get('source_root')!=str(source) or not re.fullmatch('[0-9a-f]{40}',rev):refuse('runtime binding provenance mismatch')
    runtime=state.parent/rev
    if pin.get('path')!=str(runtime) or runtime.is_symlink():refuse('runtime binding path mismatch')
    manifest=runtime/'manifest.json'
    if not regular(manifest) or sha256(manifest)!=pin.get('manifest_sha256'):refuse('runtime binding manifest mismatch')
    data=json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('revision')!=rev or data.get('version')!=pin.get('version'):refuse('runtime binding identity mismatch')
    for name,item in data.get('files',{}).items():
        if Path(name).is_absolute() or '..' in Path(name).parts:refuse('runtime binding layout mismatch')
        path=runtime/name
        if not regular(path) or sha256(path)!=item.get('sha256'):refuse('runtime binding content mismatch')
    return rev


def quarantined_filename_hints(home,state):
    """Names can block a UUID collision, never authorize an unverified owner."""
    bound_path(home,state)
    hints=set();complete=True
    def unreadable(_error):refuse('quarantined catalog rollout directory unreadable')
    for directory in (home/'sessions',home/'archived_sessions'):
        bound_path(directory,state)
        if not directory.exists():continue
        for parent,dirs,files in os.walk(directory,followlinks=False,onerror=unreadable):
            parent=Path(parent);bound_path(parent,state)
            for name in dirs:bound_path(parent/name,state)
            for name in files:
                if not name.endswith('.jsonl'):continue
                path=parent/name
                if not regular(path):refuse('unsafe quarantined catalog rollout')
                ids=[value for value in re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',name.lower()) if UUID.fullmatch(value)]
                if not ids:complete=False
                hints.update(ids)
    return hints,complete


def isolated_homes(state, source, quarantines=None):
    sessions = state / "sessions"
    worktrees = state / "worktrees"
    bound_path(sessions,state)
    bound_path(worktrees,state)
    if not sessions.is_dir() or sessions.is_symlink():
        return []
    result = []
    for record in sessions.iterdir():
        identifier = record.name
        if not UUID.fullmatch(identifier):continue
        bound_path(record,state)
        source_file, root_file = record / "source-root", record / "session-root"
        if not regular(source_file) or not regular(root_file):
            continue
        root = Path(root_file.read_text(encoding="utf-8").strip())
        if source_file.read_text(encoding="utf-8").strip() != str(source):
            continue
        if root != worktrees / identifier or root.is_symlink() or not root.is_dir():
            if root.exists():refuse('catalog session root binding mismatch')
            continue
        native=root/'.harness/codex'
        bound_path(native,state)
        if not native.exists() and not native.is_symlink():continue
        inode = record / "root-inode"
        reason=None
        if not inode.exists() and not inode.is_symlink():reason='root_inode_missing'
        elif not regular(inode):refuse('unsafe catalog session root inode')
        elif not root_identity_matches(inode.read_text(encoding="utf-8"),root):reason='root_inode_mismatch'
        if reason:
            if quarantines is None:refuse('catalog session root inode mismatch')
            quarantines.append((native,identifier,reason));continue
        result.append((root / ".harness" / "codex", identifier,binding_origin(state,identifier,source),"live"))
    return result


def home_entries(home, source, isolation, origin, status, snapshot_id):
    if status=='canonical':bound_path(home,source)
    elif status=='live':bound_path(home,home.parent.parent.parent.parent)
    elif status=='snapshot':bound_path(home,home.parent.parent.parent)
    if home.is_symlink() or not home.is_dir():
        return []
    for directory in (home/'sessions',home/'archived_sessions'):
        bound_path(directory,home)
        if directory.is_dir():
            for path in directory.rglob('*'):
                if path.is_dir() or path.is_symlink():bound_path(path,home)
    values = [];selected=None;lineage=None
    imported = set()
    if isolation and isolation!='canonical' and UUID.fullmatch(isolation) and status=='live':
        proof = native_import_provenance(home,source,isolation)
        if proof is not None:imported = imported_native_tuples(proof['imports'],isolation)
        state=home.parent.parent.parent.parent
        receipt=state/'sessions'/isolation/'native-history-restore.json'
        if receipt.exists():
            if not regular(receipt):refuse('unsafe restore lineage')
            lineage=json.loads(receipt.read_text(encoding='utf-8'));selected=lineage.get('native_id')
            if not UUID.fullmatch(str(selected)):refuse('invalid restore lineage native UUID')
    paths=list((home/'sessions').rglob('*.jsonl') if (home/'sessions').is_dir() else [])+list((home/'archived_sessions').rglob('*.jsonl') if (home/'archived_sessions').is_dir() else [])
    family_times={}
    for path in paths:
        if not regular(path):refuse('unsafe catalog rollout')
        identifiers=re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',path.name.lower())
        if identifiers:
            identifier=identifiers[-1];family_times[identifier]=max(family_times.get(identifier,0),path.stat().st_mtime_ns)
    for path in paths:
        try:
            with path.open(encoding='utf-8') as stream:
              for line in stream:
                if not line.strip():continue
                row = json.loads(line)
                payload = row.get("payload") if isinstance(row, dict) else None
                if row.get("type") == "session_meta" and isinstance(payload, dict) and UUID.fullmatch(str(payload.get("id", ""))):
                    if payload['id'].lower() in imported:break
                    if selected and payload['id']!=selected:break
                    values.append({"native_id": payload["id"], "home": str(home), "source_root": str(source),
                                   "isolation_id": isolation, "origin_runtime": origin, "snapshot_id": snapshot_id,
                                   "updated_at": str(max(path.stat().st_mtime_ns,family_times.get(payload['id'].lower(),0))), "status": status, "reason": None,
                                   "source_local": True})
                    if lineage:
                        values[-1]['_lineage']=lineage['snapshot_id']
                        values[-1]['origin_runtime']=lineage.get('origin_runtime') or origin
                    break
        except (OSError, UnicodeError, json.JSONDecodeError):
            refuse('malformed catalog rollout')
    return values


def snapshot_entries(store, source):
    snapshots = store / "snapshots"
    bound_path(snapshots,store.parent)
    if snapshots.is_symlink() or not snapshots.is_dir():
        return []
    values = []
    for path in snapshots.iterdir():
        if path.name.startswith(('.snapshot-','.import-')):
            continue
        manifest = path / "manifest.json"
        if not regular(manifest):
            continue
        archive,value=load_snapshot(store,path.name)
        if value.get("source_root") != str(source):
            continue
        rows=home_entries(path / "data", source, value.get("isolation_id"), value.get("runtime_revision"),
                          "snapshot", value.get("snapshot_id"))
        imported = snapshot_import_ids(value)
        rows = [row for row in rows if row['native_id'].lower() not in imported]
        if value.get('restored_native_id'):rows=[row for row in rows if row['native_id']==value['restored_native_id']]
        for row in rows:
            row['_source_home']=value['source'];row['_manifest']=value
            row['_store']=str(store);row['_state_home']=str(store.parent)
            row['_capture_time']=(path/'manifest.json').stat().st_mtime_ns
            if value.get('origin_snapshot'):row['_lineage']=value['origin_snapshot'];row['origin_runtime']=value.get('origin_runtime',row['origin_runtime'])
        values.extend(rows)
    return values


def inventory_signature(home,children):
    """Stat-only fingerprint of every artifact source_inventory reads or lists."""
    rows=[]
    def add(path):
        info=path.lstat()
        rows.append((path.relative_to(home).as_posix(),info.st_dev,info.st_ino,info.st_mode,info.st_size,info.st_mtime_ns,info.st_ctime_ns))
    for path in home.iterdir():add(path)
    for child in children:
        if child.is_dir():
            for path in child.rglob('*'):add(path)
    return tuple(sorted(rows))


def dedupe_entries(entries,store,keep_private=False):
    """Decide every row; each home's stat signature is walked once when first
    needed and once after all rows are decided. A home that changed in between
    is inventoried again and the dedupe is redone once; a home that changes
    again holds every row that used it."""
    state={}
    def inventory(home,used):
        home=Path(home);used.add(home)
        hit=state.get(home)
        if hit is None:
            children=source_children(home)
            signature=inventory_signature(home,children)
            hit=state[home]=(children,signature,source_inventory(home,children))
        return hit[2]
    def changed_homes():
        return {home for home,(children,signature,_) in state.items()
                if inventory_signature(home,children)!=signature}
    decided=decide_entries(entries,store,inventory)
    unstable=changed_homes()
    if unstable:
        for home in unstable:del state[home]
        decided=decide_entries(entries,store,inventory)
        unstable=changed_homes()
    result=[]
    for row,used in decided:
        if used&unstable:row.update(status='ambiguous',reason='source_changed_during_catalog')
        result.append(row if keep_private else {key:value for key,value in row.items() if not key.startswith('_')})
    return sorted(result,key=lambda row:row['native_id'])


def decide_entries(entries,store,inventory_of):
    """One dedupe pass; returns (row, homes whose inventory it consulted) pairs."""
    used=set()
    def inventory(home):return inventory_of(home,used)
    groups={}
    for row in entries:groups.setdefault(row['native_id'],[]).append(row)
    result=[]
    for identifier,rows in groups.items():
        used.clear()
        superseded=set();lineage_valid=True
        for entry in rows:
            if not entry.get('_lineage'):continue
            _,parent=load_snapshot(Path(entry.get('_store',store)),entry['_lineage'])
            owner=entry.get('_source_home',entry['home'])
            if parent['source_root']!=entry['source_root'] or parent['source']==owner:lineage_valid=False;continue
            original=Path(parent['source'])
            if original.exists():
                current=[r for r in inventory(original) if not r.get('excluded')]
                saved=[r for r in parent['source_inventory'] if not r.get('excluded')]
                if current!=saved:lineage_valid=False;continue
            superseded.add(parent['source'])
        candidates=[entry for entry in rows if entry.get('_source_home',entry['home']) not in superseded] if lineage_valid else rows
        live={row['home']:row for row in candidates if row['status']!='snapshot'}
        archives=[row for row in candidates if row['status']=='snapshot']
        # A restored home owns just the explicitly selected UUID. Its immutable
        # source lineage distinguishes it from an uncontrolled duplicate.
        restored=[row for row in live.values() if row.get('_lineage')]
        if restored:
            valid=True
            origins=set()
            for row in restored:
                _,manifest=load_snapshot(Path(row.get('_store',store)),row['_lineage']);origins.add(manifest['source'])
                if manifest['source_root']!=row['source_root']:valid=False
            candidates=[row for row in live.values() if row['home'] not in origins]
            if valid and len(candidates)==1:live={candidates[0]['home']:candidates[0]}
        if len(live)>1:
            row=next(iter(live.values())).copy();row.update(status='ambiguous',reason='duplicate_live_owner')
        elif live:
            row=next(iter(live.values())).copy()
            matches=[a for a in archives if a.get('_source_home')==row['home'] and a['isolation_id']==row['isolation_id']]
            for archive in sorted(matches,key=lambda a:(int(a['updated_at']),a['_capture_time']),reverse=True):
                manifest=archive['_manifest'];home=Path(row['home'])
                if inventory(home)==manifest['source_inventory']:
                    row['snapshot_id']=archive['snapshot_id'];row['origin_runtime']=archive['origin_runtime'];break
        else:
            owners={(a.get('_source_home'),a['isolation_id']) for a in archives}
            row=max(archives,key=lambda a:(int(a['updated_at']),a['_capture_time'])).copy()
            if len(owners)>1:row.update(status='ambiguous',reason='duplicate_archive_owner')
        if not lineage_valid:row.update(status='ambiguous',reason='diverged_restore_origin')
        result.append((row,set(used)))
    return result


def recorded_source(path, root):
    value = path.read_text(encoding="utf-8").strip()
    if (not value.startswith("/") or value.startswith("//") or os.path.normpath(value) != value
            or Path(value) in (root.absolute(), *root.absolute().parents)):
        refuse("unsafe recorded source root")
    return value


def protect_pool(args):
    # Every profile shares the old pool and its GC, so each record is protected
    # under its own recorded source root; --source-root names only the caller.
    state = Path(args.state_home)
    worktrees = state / "worktrees"
    sessions = state / "sessions"
    bound_path(worktrees, state)
    bound_path(sessions, state)
    if not worktrees.exists() and not sessions.exists():
        print(json.dumps({'schema_version':1,'safe_to_gc':True}));return
    if not worktrees.is_dir() or worktrees.is_symlink() or not sessions.is_dir() or sessions.is_symlink():
        refuse("invalid session state home")
    # Older GC removes tombs without consulting their leases. Recover each
    # recognized, owner-bound Native tomb first; never bless an unknown name.
    for tomb in worktrees.glob('.*-*'):
        if not tomb.is_dir() and not tomb.is_symlink():continue
        native=tomb/'.harness/codex'
        if not native_presence(native, state):continue
        match=re.fullmatch(r'\.(?:retired|archiving)-([0-9a-fA-F-]{36})-(\d+)',tomb.name)
        if not match or tomb.is_symlink():refuse('unverified Native tomb')
        identifier=match.group(1);record=sessions/identifier;expected=worktrees/identifier
        bound_path(record, state)
        lock=record/'runtime.lock'
        if expected.exists() or expected.is_symlink() or not all(regular(record/name) for name in ('source-root','session-root','root-inode','journal','lease-v1','runtime.lock')):
            refuse('Native tomb owner is unverified')
        recorded_source(record/'source-root', expected)
        if (record/'session-root').read_text().strip()!=str(expected) or not root_identity_matches((record/'root-inode').read_text(),tomb):refuse('Native tomb binding mismatch')
        with runtime_lease(lock):
            if not terminal_journal(record/'journal',record):refuse('Native tomb is not terminal')
            os.rename(tomb,expected)
    for record in sessions.iterdir():
        bound_path(record, state)
        if not UUID.fullmatch(record.name) or record.is_symlink() or not record.is_dir():
            refuse("unsafe session record")
        root_file, source_file, lock, journal, inode = record / "session-root", record / "source-root", record / "runtime.lock", record / "journal", record / "root-inode"
        root=worktrees/record.name
        native=root/'.harness/codex'
        if not native_presence(native, state):continue
        if not all(regular(path) for path in (root_file, source_file, lock, journal, inode,record/'lease-v1')):
            refuse("unverified session record")
        owner = recorded_source(source_file, root)
        root = Path(root_file.read_text(encoding="utf-8").strip())
        if root != worktrees / record.name or root.is_symlink() or not root.is_dir():
            refuse("session root binding mismatch")
        if not root_identity_matches(inode.read_text(encoding="utf-8"), root):
            refuse("session root inode mismatch")
        if not terminal_journal(journal,record):
            continue
        if not args.codex_bin:refuse('Native binary unavailable for pool proof')
        native = root / ".harness" / "codex"
        if not native_presence(native, state):
            continue
        with runtime_lease(lock):
            if not terminal_journal(journal,record) or (record/'lease-v1').read_text()!='1\n' or not root_identity_matches(inode.read_text(),root):refuse('Native candidate changed')
            receipt = record / ('native-history-protect-'+uuid.uuid4().hex+'.json')
            snapshot_args = argparse.Namespace(source=str(native), catalog=str(state / "native-history"), receipt=str(receipt),
                                               source_root=owner, isolation_id=record.name, runtime_revision="legacy-unknown")
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured): snapshot(snapshot_args)
            snapshot_id = json.loads(captured.getvalue())["snapshot_id"]
            tomb = worktrees / (".archiving-" + record.name + "-" + str(os.getpid()))
            if tomb.exists() or tomb.is_symlink():
                refuse("archiving tomb already exists")
            os.rename(root, tomb)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    verify(argparse.Namespace(catalog=str(state / "native-history"), snapshot=snapshot_id,
                                              source=str(tomb / ".harness" / "codex"), codex_bin=args.codex_bin))
            finally:
                if tomb.exists() and not root.exists(): os.rename(tomb, root)
    # Every Native-bearing unrecorded directory would also be an old GC target.
    for root in worktrees.iterdir():
        if not root.is_dir() and not root.is_symlink():continue
        if native_presence(root/'.harness/codex', state) and not (sessions/root.name).is_dir():refuse('unbound Native root')
    print(json.dumps({"schema_version": 1, "safe_to_gc": True}, sort_keys=True))


def import_snapshot(origin, destination, identifier, source, visiting=None):
    """Copy a verified source-local ancestor closure without retiring its origin."""
    visiting=set() if visiting is None else visiting
    if identifier in visiting:refuse('cyclic snapshot lineage')
    visiting.add(identifier)
    root,manifest=load_snapshot(origin,identifier)
    if manifest['source_root']!=str(source):refuse('snapshot import crosses source boundary')
    if inventory(root/'data')!=manifest['archive_inventory']:refuse('snapshot import content mismatch')
    parent=manifest.get('origin_snapshot')
    if parent:import_snapshot(origin,destination,parent,source,visiting)
    if origin!=destination:
        secure_dir(destination);snapshots=destination/'snapshots';secure_dir(snapshots)
        target=snapshots/identifier
        if target.exists():
            _,saved=load_snapshot(destination,identifier)
            if saved!=manifest or inventory(target/'data')!=manifest['archive_inventory']:refuse('imported snapshot conflicts')
        else:
            staging=Path(tempfile.mkdtemp(prefix='.import-',dir=snapshots))
            try:
                secure_dir(staging/'data')
                for item in manifest['archive_inventory']:
                    copy_regular(root/'data'/item['path'],staging/'data'/item['path'])
                copy_regular(root/'manifest.json',staging/'manifest.json')
                sync_tree(staging);os.rename(staging,target)
                descriptor=os.open(snapshots,os.O_RDONLY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            finally:
                if staging.exists():shutil.rmtree(staging)
    visiting.remove(identifier)


def prepare(args):
    source = Path(args.source_root).absolute()
    if not UUID.fullmatch(args.native_id):
        refuse("invalid selected native UUID")
    selected=[row for row in catalog_value(args,True)['entries'] if row['native_id']==args.native_id and row['status'] in ('canonical','live','snapshot') and not row['reason']]
    if len(selected) != 1:
        refuse("selected native history is absent or ambiguous")
    catalog_dir = Path(args.state_home) / "native-history"
    receipt_dir = catalog_dir / "receipts"; secure_dir(receipt_dir)
    row=selected[0]
    origin_store=Path(row['_store'])
    if row['status']=='snapshot':
        import_snapshot(origin_store,catalog_dir,row['snapshot_id'],source)
        row=next(r for r in catalog_value(args)['entries'] if r['native_id']==args.native_id)
        print(json.dumps(row,sort_keys=True));return
    home=Path(row['home']);receipt=receipt_dir/(args.native_id+'.'+uuid.uuid4().hex+'.prepare.json')
    def take_snapshot():
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            snapshot(argparse.Namespace(source=str(home), catalog=str(origin_store), receipt=str(receipt),
                                        source_root=str(source), isolation_id=row['isolation_id'], runtime_revision=row['origin_runtime'] or 'legacy-unknown'))
        return json.loads(capture.getvalue())
    if row['status']=='live':
        record=Path(row['_state_home'])/'sessions'/row['isolation_id']
        with runtime_lease(record/'runtime.lock'):
            if not terminal_journal(record/'journal',record):raise ActiveWriter('original Native session is not terminal')
            result=take_snapshot()
    else:result=take_snapshot()
    import_snapshot(origin_store,catalog_dir,result['snapshot_id'],source)
    row=next(r for r in catalog_value(args)['entries'] if r['native_id']==args.native_id)
    print(json.dumps(row, sort_keys=True))


def root_inode(path):
    value = path.stat()
    return "%s %s" % (value.st_dev, value.st_ino)


def identity_stat(path):
    return os.lstat(path)


def root_identity_matches(recorded, path):
    """The record "<dev> <ino>" still names `path`, a real directory (never followed).

    The inode must match. The device must match too, except that a whole-volume
    device renumbering (reboot) is accepted when the root sits on the device of
    its parent (the worktrees directory); a root replaced by another filesystem
    has a different device from its parent and is refused.
    """
    fields = str(recorded).split()
    if len(fields) != 2 or not all(re.fullmatch(r"[0-9]+", field) for field in fields):
        return False
    device, inode = int(fields[0]), int(fields[1])
    try:
        path = Path(path)
        info = identity_stat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_ino != inode:
            return False
        if info.st_dev == device:
            return True
        return identity_stat(path.parent).st_dev == info.st_dev
    except OSError:
        return False


@contextlib.contextmanager
def runtime_lease(path):
    if not regular(path):refuse('unsafe runtime lease')
    descriptor=os.open(path,os.O_RDWR|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:
        try:fcntl.lockf(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:raise ActiveWriter('original session lease is active') from error
        yield
    finally:os.close(descriptor)


def terminal_journal(path,record=None):
    try:
        rows=path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError, IndexError):
        return False
    if len(rows)!=3 or not re.fullmatch(r'heartbeat=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',rows[2]):return False
    if rows[0]=='state=CLOSED':return rows[1]=='identity='
    if record is None:return False
    if rows[0]=='state=DISCARDED':return regular(record/'discarded.patch') and bool(re.fullmatch(r'identity=(?:[0-9a-fA-F]{64})?',rows[1]))
    if rows[0]!='state=DELIVERED' or not re.fullmatch('identity=[0-9a-fA-F]{64}',rows[1]):return False
    if not all(regular(record/name) for name in ('delivered-sha','delivered-manifest')):return False
    if not re.fullmatch(r'[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\n',(record/'delivered-sha').read_text()):return False
    pieces=(record/'delivered-manifest').read_bytes().split(b'\0')
    if len(pieces)<5 or pieces[-1]!=b'' or (len(pieces)-1)%4:return False
    for offset in range(0,len(pieces)-1,4):
        kind,mode_,oid,path_=pieces[offset:offset+4]
        if kind==b'F':
            if mode_ not in (b'100644',b'100755',b'120000',b'160000') or not re.fullmatch(rb'[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?',oid):return False
        elif kind==b'D':
            if mode_!=b'-' or oid!=b'-':return False
        else:return False
        if not path_ or path_.startswith(b'/') or any(part in (b'',b'.',b'..') for part in path_.split(b'/')):return False
    return True


def parser():
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    presence_parser = commands.add_parser("presence")
    for name in ("source", "anchor"):
        presence_parser.add_argument("--" + name, required=True)
    snap = commands.add_parser("snapshot")
    for name in ("source", "catalog", "receipt", "source-root", "isolation-id", "runtime-revision"):
        snap.add_argument("--" + name, required=True)
    restore_parser = commands.add_parser("restore")
    for name in ("catalog", "snapshot", "destination", "receipt"):
        restore_parser.add_argument("--" + name, required=True)
    restore_parser.add_argument("--native-id")
    restore_parser.add_argument("--origin-runtime")
    verify_parser = commands.add_parser("verify")
    for name in ("catalog", "snapshot", "source", "codex-bin"):
        verify_parser.add_argument("--" + name, required=True)
    catalog_parser = commands.add_parser("catalog")
    catalog_parser.add_argument("--source-root", required=True)
    catalog_parser.add_argument("--state-home", required=True)
    catalog_parser.add_argument("--legacy-state-home")
    catalog_parser.add_argument("--native-id")
    pool = commands.add_parser("protect-pool")
    for name in ("state-home", "source-root"):
        pool.add_argument("--" + name, required=True)
    pool.add_argument('--codex-bin')
    prepare_parser = commands.add_parser("prepare")
    for name in ("source-root", "state-home", "native-id"):
        prepare_parser.add_argument("--" + name, required=True)
    prepare_parser.add_argument('--legacy-state-home')
    return root


def main(argv):
    args = parser().parse_args(argv)
    return {"snapshot": snapshot, "restore": restore, "verify": verify,
            "catalog": catalog, "protect-pool": protect_pool, "prepare": prepare,
            "presence": presence}[args.command](args)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Refusal as error:
        print(str(error), file=sys.stderr)
        sys.exit(error.code)
