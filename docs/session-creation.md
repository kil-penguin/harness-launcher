# Concurrent session creation

Registered profiles share the launcher, while each new session has its own
creation lease and workspace. Creating one profile's session must not hold a
global activation lock or serialize another profile's clone.

## Preparation and admission

`harness_session_creation.py` owns the versioned creation protocol. Before
fetch or clone it reserves a new UUID, records the canonical source and executing
runtime identity, and holds a creation lease. A child inherits that lease so a
parent's death cannot make a still-running child look abandoned.

The internal reserved create path verifies the inherited descriptor and owned
reservation before writing its workspace. Ordinary `harness-session create`
retains its existing arguments and four `HARNESS_*` output fields and uses the
same protocol in standalone mode.

A prepared workspace has no normal session journal. Admission publishes the
existing `OPEN` journal only after preparation and any caller-owned runtime
binding are complete. This also fences older resume implementations that do not
understand the new creation marker. Existing sessions keep their original
journals, leases and immutable runtime bindings.

## Binding and recovery

A caller supplying an expected runtime pin must persist the complete pin and
verify the binding before committing creation. The readback result distinguishes
`MATCH`, `ABSENT`, `CONFLICT` and `UNKNOWN`; a failed read is never absence.
`MATCH` must agree on the canonical source and complete expected runtime pin.
Standalone creation has no external binding and verifies its own preparation.

Before publication intent, a failed invocation may clean up only its proven own
unadmitted artifacts after its child has stopped. After publication intent,
uncertainty retains the reservation for readback. A valid published journal,
changed identity, live lease or unknown binding prevents destructive cleanup.

Cancellation and timeout signal the owned engine group before reaping its
leader. A non-reaping wait keeps that identity reserved through the bounded
TERM grace and final KILL; no group signal follows child reaping. Repeated
interrupts cannot bypass shutdown. An unconfirmed shutdown retains the
reservation and reports a fixed phase diagnostic without replacing the original
timeout or cancellation. Preparation runs on the main thread so its scoped
interrupt handler can prevent implicit reaping inside `communicate()`.

Reconciliation uses a source-owned pending index rather than walking the legacy
session inventory. Inspection is bounded and reports when its limit is reached.
It acquires the actual creation
lease before inspecting abandoned work; repeated recovery is idempotent. A
completion response lost after publication is resolved by checking durable
state. The protocol never deletes committed or running sessions merely because
their original creator exited.

## Contention and compatibility

Only fetch and exact base selection for the same physical source repository
share a source lock. Clone and preparation run independently. Lock waiting has
a separate ten-second budget from the creation operation's 120-second budget.
Wait diagnostics identify the phase without logging commands or credentials.

The profile dispatcher selects a verified immutable runtime without waiting
for activation and publishes bindings under UUID-local coordination. Runtime
activation retains its existing prepare, validation and rollback transaction;
private snapshot copying and hashing happen outside that transaction.

The dispatcher uses this helper only when its verified runtime ships the
supported capability. Older pinned runtimes retain their original create
grammar. Update the engine and dispatcher together and verify every registered
profile; a package version alone does not prove the shell's dispatcher changed.

The session-creation and adversarial suites exercise the real filesystem, OS
leases and child processes. Both are in the full runner and Python CI lane.
