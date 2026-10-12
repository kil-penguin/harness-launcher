# Durable native Codex history

Native conversation artifacts are data. Generated configuration and disposable
Git workspaces have different lifetimes. This design replaces the claim that
all generated runtime-home contents are disposable; it adds no always-on agent
rule, service or daemon.

## Boundaries

- Archives are host-local, source/profile-bound and owner-private (0700/0600).
- Keep original native/isolation UUIDs, source home and runtime provenance.
- Never copy auth, generated config, hook trust, plugins or live process locks.
  The same holds for execpolicy `rules/` (command-approval authority), the exact
  Native app, daemon and tool state names `app-server-control`,
  `app-server-daemon`, `process_manager`, `computer-use`, `vendor_imports`,
  `ambient-suggestions`, `.codex-global-state.json`, `.codex-global-state.json.bak`,
  `.cmux-title-sync` and `.glider`, Finder `.DS_Store`, and interrupted `mktemp`
  siblings of generated files (`.AGENTS.md.XXXXXX`, `.config.toml.XXXXXX`,
  `.plugin-<name>.XXXXXX`, ...). A six-letter word suffix matches that pattern
  too; every matched prefix names generated config, never history. The generic
  `sqlite/` and `tui-luna-reserve/` names are excluded only while they hold the
  app's `codex-dev.db` (automations and inbox) or nothing. Excluded entries are
  not archived, so retiring a root discards them. That state and qualifying
  generic directories are inventoried by name and kind only: content changes do
  not hold a snapshot, but an entry appearing, disappearing or changing kind does.
- Preserve complete sessions and archived sessions, including segmented rollouts,
  parent history, attachments, shell snapshots, memories and native databases.
  `external_agent_session_imports.json` travels with them so an imported
  external session is not imported again.
- Classify every top-level entry. Unknown data, active writers, invalid SQLite,
  symlinks, changed source, missing provenance or incomplete history block deletion.
- Read-only SQLite backup includes committed WAL data. A WAL file alone does
  not prove a live writer; independent database backups do not prove the whole
  home was stable. Check the complete source inventory before and afterward.

## Internal interfaces

`bin/codex-history.py` provides on-demand `snapshot`, `restore`, `verify`,
`catalog`, `presence` and `protect-pool` operations; it is not a new public executable.

- `snapshot --source HOME --catalog STORE --receipt RECEIPT --source-root ROOT
  --isolation-id UUID --runtime-revision REV` records immutable native data,
  source inventory, provenance and content hashes. `REV` is a full Git SHA or
  the explicit `legacy-unknown` value, never an inferred pin.
- `restore --catalog STORE --snapshot ID --destination HOME --receipt RECEIPT`
  writes native artifacts into a fresh home without overwriting existing native
  files or importing authority. Generated files may already be present.
- `verify --catalog STORE --snapshot ID --source SOURCE_HOME --codex-bin BIN`
  checks the saved hashes and a fresh derived restore. Original source paths
  must be inaccessible during the native full-history read. Only derived
  databases may relocate schema-known home paths; originals remain untouched.
- `catalog --source-root ROOT --state-home STATE [--legacy-state-home STATE]`
  reports native UUID, source/home/isolation, status, time and origin pin from
  canonical, live isolated and validated archived data. No conversation body,
  secret or grant appears in default JSON output. Ambiguous owners fail closed.
  Missing or mismatched original root-inode records produce metadata-only
  `warnings` for that candidate; other verified homes remain discoverable.
  Quarantined bodies are not parsed, their roots cannot be prepared or retired,
  and filename UUID collisions keep that UUID ambiguous. Incomplete filename
  hints block selection rather than guessing an owner.
- `prepare --source-root ROOT --state-home STATE --native-id UUID` snapshots a
  selected stable canonical or terminal isolated home. Canonical provenance uses
  the literal `canonical` marker and exact source home, never a fabricated Git
  UUID. A Native UUID lease spans preparation, fresh creation and Native exit.
- `protect-pool --state-home STATE --source-root ROOT --codex-bin BIN` verifies
  every terminal native-bearing root regardless of age, plus every old tomb,
before an immutable older runtime can execute its GC. Unknown or unverified
  candidates refuse that launch; old-runtime retention has a 24-hour floor.
  An explicit `archive` retention sentinel remains intact so numeric-only
  older engines refuse deletion.
- `presence --source HOME --anchor STATE` returns `native` or `absent` only
  after validating accessible, owned directory ancestry. Permission errors,
  redirected paths and failed checks hold the original root or old tomb.

## Retirement and recovery

GC first takes the existing runtime lease. It snapshots stable terminal data,
moves the original root to recoverable `.archiving-*` staging, and verifies the
restored history without access to the original pathname. Failure restores the
root; crashes retain staging. Delete only after a durable successful native
restore receipt and final source validation. Old `.retired-*` remnants receive
the same protection; a filename is not deletion authorization.
The managed conversation catalog also retains its verified transcript archive.
That transcript backup alone does not authorize Native root deletion. Missing
original inode provenance or an invalid imported-parent ledger holds the root
before Native data is copied. Valid imported-parent source-owner tuples and
ledger hashes remain in Native snapshots; imported parents do not become new
owners in either live or archived Native catalogs.
Fresh Native restores retain all context originals but assign ownership only to
the selected UUID. The managed catalog validates the private restore receipt and
its source-bound, content-addressed Native manifest before reading transcripts.
Its transcript archive preserves that selection after the live home is removed;
changed receipt metadata or a different selection blocks archive reuse.
Existing transcript-only archives remain available through the managed
conversation catalog. The Native CLI catalog discovers its verified Native
snapshots; conversion of older transcript-only archives is not performed.

Do not reopen DELIVERED or DISCARDED journals to recover conversation data.
Historical native resumes use a fresh owned Git session and compatible verified
runtime. Origin UUID/pin remain provenance, separately from the new execution
UUID/pin. Running sessions and their bindings remain unchanged. The explicit
history-restore path uses `session-isolation.sh create --local` with a locally
available cached origin/main (or local main for a repository without origin),
records that exact base, and never fetches. Ordinary new-session creation keeps
its existing fetch behavior; delivery still uses the normal remote verifier.

## User behavior and verification

`<prefix> codex resume` lists locally discoverable history before selection;
`resume --list` provides JSON without launching a model. Exact native UUID and
`continue` use the same source-local catalog. Noninteractive ambiguous requests
require an exact ID. Network and the summary service are not prerequisites.

Tests must cover lossless WAL and parent/segment restoration after the original
path disappears, all GC deletion paths, old-pin entry protection, writer and
profile boundaries, authority exclusion, failure recovery, offline selection,
and real native history reads without a model prompt. Fixture verification is
separate from operational host migration and authenticated model turns.

The initial deletion verifier characterizes Native **0.161.0** only. Other
versions keep roots until their history protocol is verified. The proof compares
every original rollout byte and all original projection items; it does not
reconstruct projection items already missing from the original database or
recover data removed before an archive existed. Source, archive and restoration
are host-local; installing the launcher does not synchronize conversations
between machines. Existing auth and hook approvals come from normal preparation.
