# Durable session integration

The optional queue separates durable acceptance from verified delivery. It
does not shorten repository tests or promise a completion deadline. Unenrolled
pools retain the synchronous legacy broker. Enrolled pools use the same queue
for synchronous close, asynchronous acceptance, recovery, discard and GC.

## Explicit cutover

Upgrade the command/runtime used for delivery first. Inventory every old shared
pool and new profile pool that can write the same destinations. Finish old
writers before enabling; never kill them or move/rewrite session roots as an
implicit migration. `queue enable` refuses a busy legacy lock. Enroll **all**
affected pools before starting the coordinator; registration/config changes
refuse while its execution lease is held, including by a surviving child.

```bash
QUEUE_HOME="$HOME/.local/state/harness-launcher/integration"
HARNESS_SESSION_STATE_HOME=/absolute/old-pool harness-session queue enable --home "$QUEUE_HOME"
HARNESS_SESSION_STATE_HOME=/absolute/profile-pool harness-session queue enable --home "$QUEUE_HOME"
harness-session queue service-plist --home "$QUEUE_HOME"
harness-session queue service-install --home "$QUEUE_HOME"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.harness-launcher.integration.plist"
```

`service-plist` only prints XML. `service-install` writes an owned private plist
but does not bootstrap or stop anything. Its absolute interpreter and immutable
runtime remain pinned across package upgrades. Ordinary launch/close/report
commands never enroll a pool, install a plist or start a daemon. Without
`--home`, enable uses `$XDG_STATE_HOME/harness-launcher/integration`, falling
back to `$HOME/.local/state/harness-launcher/integration`.

## Commands and results

```bash
harness-session close --enqueue <session-uuid>
harness-session queue status <job-uuid>
harness-session wait <job-uuid> --timeout 60
harness-session close --wait <session-uuid>
harness-session queue serve --home "$QUEUE_HOME" --once
```

`accepted: true` means the input snapshot and ledger transaction are durable.
It does not mean pushed or delivered, or that a worker is online. Repeated
registration of the same frozen submission returns the same job. Waiting
timeout exits 124 without cancelling it. Default close and headless delivery
keep their synchronous completion contract through enqueue+wait. A clean
close still records CLOSED without a job. JSON status includes job state,
sequence, attempt, phase, queue wait, phase elapsed time, worker/coordinator PIDs
and return code; PIDs are observations, never ownership proof. Full verifier
output is in `<queue-home>/logs/<job-uuid>.log`. Repository-owned verifiers
remain responsible for individual suite progress and timeout policies.

## Ownership, budgets and recovery

SQLite FULL-synchronous transactions publish accepted jobs only after fsynced,
hash-bound patch/manifest/binding/headless/message snapshots and runtime pins.
Files are private; redirected/special/hard-linked inputs refuse. Inputs are
limited to 64 MiB per file. Snapshots/pins are retained without automatic expiry.
No caller credentials or arbitrary environment are persisted. Canonical origin
matching, trusted repository verification, non-force pushes and fresh remote
manifest proof remain required. Local origin aliases and trusted OpenSSH host
aliases share destination identity; resolving SSH configuration makes no connection.

The coordinator acquires **every enrolled pool's original integration.lock**
before claiming any work. It retains these compatibility gates while serving,
blocking old broker/GC paths while queue-aware jobs proceed per destination.
FIFO applies to start order per destination, across all enrolled pools. Worker
and per-session/pool leases stay with trusted execution children and a ready
guardian, including every enrolled pool's gate; candidate verifiers inherit
neither gate FDs nor queue authority.
Stale heartbeats never transfer ownership. An exited shell does not release a budget
slot until its owned process group and inherited session lease are quiescent.
GC takes an exclusive pool-operation lease; queued jobs and live executions
prevent retirement races. Old absolute roots and immutable bindings stay fixed.

Defaults are one worker and one test slot. Explicit `queue enable --jobs N
--test-jobs M` allows up to four workers and sixteen combined test slots;
measure host load before increasing either. GC/recover/discard also consume
worker slots. Stop claims with `launchctl bootout gui/<uid>/local.harness-launcher.integration`;
owned groups receive TERM then KILL if required, with remote uncertainty kept
for reconciliation. A killed coordinator cannot transfer its leases while a
child retains them. Reboot/restart checks pending SHA, acknowledgement and fresh
remote content before any retry. Candidate SHA, manifest and journal are flushed
before a durable publishing intent and push. Missing proof after publishing
retains uncertainty instead of resubmitting. Two automatic execution attempts bound crash
recovery; unresolved jobs stay needs_reconcile. Inspect status/logs, then use
`queue reconcile <job-uuid>` to explicitly permit another reconciliation cycle.
Never delete locks, ledger entries or pending evidence to force progress.

For rollback, boot out the service, finish/reconcile accepted jobs, then run
`queue disable --home "$QUEUE_HOME"`. It refuses pending/uncertain jobs or live
owners, atomically excludes new admissions before removing all registered pool markers, and keeps
the ledger/snapshots for audit. Interrupted disable must finish before re-enable.
Never manually remove markers or change engines while work remains. Unavailable
legacy gates leave jobs queued; one-shot serve success does not prove delivery.
