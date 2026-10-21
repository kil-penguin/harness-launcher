# Read-only session disk diagnostics

Run an explicit report before investigating retained session roots:

```sh
HARNESS_SESSION_STATE_HOME=/path/to/session-state harness-session report --json
```

The report does not run automatically at startup. It never calls GC, archive,
restore, native history verification, or stale-hold cleanup. It does not create
missing state directories or update journals, leases, holds, archives, or roots.
GC keeps the default 24-hour retention and now rechecks every retained work
file against the base, verified delivery, or reconstructed discard patch under
the runtime lease. Staged-only, ignored, unknown, or later changes are retained.
The work proof makes one narrow exception for untracked CPython bytecode in
`__pycache__`: the sibling `.py` must be Git-accounted, its contents must match,
and the cache tag, magic, flags and timestamp/size or source hash must match.
Source is compiled, never imported or executed; supplied bytecode is not loaded.
If the complete cache reproduces byte for byte under the current CPython, the
proof records `exactly_reproduced`. CPython serialization details can vary even
for the same source. In that case stock GC, while holding the owner lease, may
preserve the entire original in the owner's private `python-cache-archive`.
The content-addressed bytes and a receipt with owner, source root, original
relative path, size and hash are verified and synced before the proof records
`preserved_original`. This proves custody, not bytecode trust. Existing entries
are never overwritten or automatically expired; failures keep the original root.
The archive is limited to 256 files and 64 MiB per owner. Ordinary proof calls
only verify existing preservation; the internal `--preserve-python-caches`
flag permits stock GC to create it. It does not authorize a GC invocation.
Other interpreter tags, stale or damaged caches, notes, orphan bytecode,
symlinks, and staged or modified tracked bytecode remain protected. A cache
directory is never excluded wholesale. Retention, leases and Native history
verification are unchanged; passing this work proof alone does not authorize
retirement.
This command is available after installing a release containing it;
for source-tree tests use `bash bin/session-isolation.sh report --json`.

The result includes each observed UUID's recorded lifecycle, age in seconds,
retention, expected and recorded root, root inode check, source path, base SHA,
lease observation, resume holds, archive presence, and all applicable retained
reasons. Age uses `journal.heartbeat`, the terminal transition timestamp for a
terminal journal. It does not infer terminal state from an old runtime heartbeat:
`ABANDONED`, `CONFLICT`, `SUBMITTED`, and `INTEGRATING` are not GC terminal states.
`HARNESS_SESSION_RETENTION_SECONDS` accepts the existing `0..604800` range;
`archive` means 86400 seconds.

A shared, nonblocking lock probe opens an existing `runtime.lock` read-only and
immediately releases the probe lock. It conflicts with the launcher's exclusive
lease, so an old `CLOSED` record can still report `active_lease`. A free probe is
only an observation at that instant. The current `HARNESS_SESSION_ID` receives
additional protection. Holds are reported as `present_unverified` and retained,
including apparently stale holds; the report does not inspect their process or
remove them. Invalid or inaccessible metadata remains unknown.

## Optional runtime identity

If the host uses a managed runtime selector with `current.json` and
`bindings/<uuid>.json`, supply its root explicitly:

```sh
harness-session report --json --runtime-home /path/to/managed/runtimes
```

This reads only the selector/binding metadata and checks the runtime directory
path. It reports bound and selected revision, version, manifest hash, path,
source-path agreement, and whether the selection differs. These are
`metadata_only` observations, not full snapshot or Git provenance verification.
An absent, malformed, mismatched, or inaccessible binding stays unknown or
reports a mismatch. Without this option, runtime identity remains unknown;
the command does not borrow the running executable's version for other sessions.
A newer selection never makes an older bound session safe to delete.

## Optional bounded sizes

Size scanning is opt-in:

```sh
harness-session report --json --sizes --max-nodes 10000 --max-seconds 5
```

The scan uses file metadata, skips symlinks, refuses untrusted/replaced session
roots, and does not descend into a different filesystem. It opens directories
without following symlink components and never opens file bodies in the root.
This includes native transcripts, Git objects, auth, and generated configuration.
Archive contents and native history are not read or hashed.

| Size field | Meaning |
| --- | --- |
| `nominal_regular_bytes` | Sum of regular-file logical lengths per path |
| `allocated_regular_bytes` | Sum of `st_blocks * 512` per regular-file path |
| `unique_inode_allocated_bytes` | Allocated bytes deduplicated within this root by device/inode |
| `shared_hardlink_allocated_bytes` | Deduplicated bytes for inodes with more than one hardlink, including possible links outside this root |
| `physically_reclaimable_bytes` | Always `null`; file metadata cannot establish reclaimable physical space |

Allocated figures do not account for filesystem clones, compression, snapshots,
or other filesystem sharing. Per-root figures must not be summed as physical
reclaimable space. `size_totals` is always `null`. A partial or inaccessible scan
has `null` totals and separate `observed` counters for the traversed portion.
An inaccessible root is not an empty, zero-byte root.

The shared directory-entry budget defaults to 10000 and accepts `1..100000`.
At most 1000 session-directory entries are listed (`--max-sessions 1..1000`).
The cooperative wall-time deadline defaults to 5 seconds (`--max-seconds 1..30`);
blocking filesystem calls cannot be interrupted by this deadline. Metadata files
are limited to 64 KiB and hold listings to 1000 entries per record, within the
shared budget. A hit limit, deadline, or scan error marks incomplete coverage.
Reports are observational snapshots; running processes may change files during
or after a scan. No report is a lock or authorization for a later operation.

## Conservative interpretation

`delete_safe` is always `false`. `eligibility` is `retained` when an operational
blocker is observed, otherwise `needs_provenance_recovery`. Reasons always retain
`needs_provenance_recovery` and `needs_native_verify`, since this metadata-only
command cannot establish complete source/runtime provenance or Native history
and archive fidelity. Archive `verification` remains `unknown`, even when an
archive directory exists. Full verification would require a separate workflow.

`last_gc_trigger` is `null` with `last_gc_trigger_evidence` explaining that no
durable trigger record was read. Directory timestamps and session counts do not
prove when GC ran or whether it failed.

One abbreviated synthetic result:

```json
{
  "schema_version": 1,
  "read_only": true,
  "scan_status": "complete",
  "last_gc_trigger": null,
  "sessions": [{
    "session_id": "11111111-1111-4111-8111-111111111111",
    "lifecycle": "CLOSED",
    "age_seconds": 172800,
    "retention_seconds": 86400,
    "lease": "active",
    "eligibility": "retained",
    "delete_safe": false,
    "retained_reasons": [
      "active_lease",
      "needs_provenance_recovery",
      "archive_verification_unknown",
      "needs_native_verify"
    ]
  }]
}
```

Missing or unsafe state produces JSON with `scan_status: "unknown"` and problems;
other incomplete coverage uses `partial`. Inspect these fields even on exit 0.
Invalid CLI options or retention values exit 2. JSON contains local identity paths
and is intended for local diagnostics; public examples should use synthetic data.

## Managed maintenance

Interactive and non-interactive agent startup do not scan the pool or run GC.
A verified management entry can invoke `maintain --report-only` (the default)
or the separately approved `maintain --apply`. It scans registered source pools
plus legacy/shared pools, writes a private durable before-report, and starts at
most ten targeted owners within a 300-second new-work budget. A host sweep lock
and the existing owner leases serialize work. An in-flight archive is allowed
to finish; a deadline does not kill a process during a rename or deletion.

The optional host installer is owned by the management harness. Its six-hour
LaunchAgent is installed only on explicit request, defaults to report-only,
and requires a separate approval before enabling deletion. It verifies the
loaded immutable dispatcher and exact mode. Engine installation alone does not
install or activate a job. No native snapshot or transcript archive expires
automatically. Unverified Native versions and parent-history formats remain held.

New managed sessions use `session-state/<registered-profile>/`; existing UUIDs
keep their recorded absolute root and immutable binding in the shared pool.
Existing roots are never moved to adopt this layout. Profiles are identified
by their registered source root, and ambiguous owners are refused.
