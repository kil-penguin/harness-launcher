# Owned lifecycle CLI (unadvertised)

This consumer remains unadvertised pending independent Native, Web, CLI and
recovery reviews. It uses existing local discovery and authenticated loopback
transport; it does not introduce an identity capability or arbitrary URL option.
Fresh Native lifecycle capability checks belong to Web on every request.

## Commands

```sh
harness-herdr-web cli owned-create --device FULL_UUID --cwd-file /private/path/cwd --new-operation
harness-herdr-web cli owned-interrupt --receipt FULL_INTENT_UUID --new-operation
harness-herdr-web cli owned-close --receipt FULL_INTENT_UUID --new-operation
harness-herdr-web cli lifecycle-status --intent FULL_INTENT_UUID
harness-herdr-web cli lifecycle-cancel --intent FULL_INTENT_UUID
```

`--device` is a full lowercase UUID, including for this computer. The cwd file
must be an owned regular 0600 single-link file in a private 0700 directory,
without symlinks in its path. Its content is read once: an absolute NUL-free
UTF-8 cwd of at most 4096 bytes. No argv, environment, shell, label, agent or
existing resource selection is accepted.

Interrupt and close require the original completed local create receipt and
its private owner proof. They create a fresh intent without modifying that
original receipt. Close derives exactly one immutable owned resource member.
The selected snapshot must have a non-null epoch matching the original receipt;
a restart never authorizes adoption of another resource or process.

## Receipts and recovery

Lifecycle records are separate from input, UI-answer and managed-launch state:
`~/.local/state/harness-herdr-web/lifecycle-receipts/v1/`.
The private `harness-herdr-web`, `lifecycle-receipts` and `v1` directories are
owned 0700; files and per-intent advisory locks are owned 0600. Existing common
`.local` and `state` parents may retain owned, non-group/world-writable modes
such as 0755. Missing parents are created 0700. Symlinks and unsafe ownership or
write permissions are refused; existing parent modes are never changed.
Records contain private owner keys and admission tickets: never print or share
these files. They contain no cwd or action body. Keep them until the operation
has been inspected and any required dependent action has finished.

Before prepare and dispatch, the private intent/reservation is durable and a
safe JSONL start line is flushed. Each invocation attempts at most one prepare
and one explicit action. Status and cancel address only the stored operation.
An unknown prepare has no operation ID and recovery makes zero HTTP requests.
A lost action is never replayed; status can recover the same completed create
receipt. A receipt persistence failure grants no interrupt/close authority
until that same status result is validated and durably recovered.

Recovery markers fence dependent receipt authority; get-only markers fence
future POST mutations after uncertainty or a mismatched response. Original marker
inode/content and bindings are pinned before each request; replacement refuses
receipt authority. A valid GET
never removes a pre-existing get-only marker. A successful known queued or
reserved action can still be explicitly cancelled. The HTTP timeout does not
mean a Native operation finished or had zero effect. Post-spawn reconciling
preserves the spawned resource and does not permit another create or cleanup.

Output is only `{intent,device_id,method}` and, after strict validation and
persistence, an `operation` with the closed lifecycle projection. Owner keys,
tickets, cwd, action bodies, session data and raw errors are never output.
