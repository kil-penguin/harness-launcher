# Codex UI request CLI

`agent-interrupt` and `agent-exit` request a single already-approved UI-equivalent
PTY key through the Web controller. They do not claim that a turn was interrupted,
that Codex exited, or that any process stopped.

```text
harness-herdr-web cli agent-interrupt --device <full-lowercase-uuid> --target-file <absolute-private-file> --new-operation
harness-herdr-web cli agent-exit --device <full-lowercase-uuid> --target-file <absolute-private-file> --new-operation
harness-herdr-web cli status --intent <intent-uuid>
harness-herdr-web cli cancel --intent <intent-uuid>
```

The target file is the existing full `guard-target` wrapper, read once before
network discovery. It must be a caller-owned, single-link mode-0600 regular file
inside a mode-0700 directory, contain the full immutable target and an observed
Codex binding, and cannot be replaced by a pane identifier. The CLI does not
accept a caller key, popup, draft, ticket, session, or body parameter.

Each new operation writes a CSPRNG intent receipt under
`~/.local/state/harness-herdr-web/agent-ui-request-receipts/v1` before the first
POST, flushes a safe JSONL start line, persists the prepared workflow before its
one dispatch POST, and never replays a lost prepare, dispatch, or cancel. Records
are owner-only, directory-FD/no-follow, atomic file-plus-parent-fsync updates.
Status and cancellation select a receipt namespace only when exactly one valid
owner-only receipt with that UUID exists. A malformed, unsafe, or duplicated UUID
across the guarded and UI-request stores fails closed; an absent UI-request
receipt preserves the pre-existing guarded status/cancel behavior. A missing
workflow ID is get/cancel-network-zero; process/controller loss leaves the
receipt unknown or dispatch-started and does not reconstruct target authority.

The Web capability must exactly advertise `web_guarded_agent_ui_request_v1` with
the four `web.agent_ui_request.*.v1` methods, `agents:["codex"]`, and
`requests:["interrupt","exit"]`. Requests and responses are bounded JSON with a
ten-second whole transport budget. JSONL output excludes target, binding, session,
ticket, popup, draft, key, request body, and upstream error text.
