# Trusted preservation

`harness-preserve` reads one strict JSON request from standard input and opens a
native, one-shot confirmation before a bounded Git preservation action. It has
no command-line approval option. Invalid input is rejected without a dialog.

The command is installed only through the supported Homebrew release flow.
A source checkout alone is not proof of a released or installed command.
It is not an authorization to preserve a repository: backups, independent
review, writer coordination, and human confirmation remain required.

Failures after Git starts may be partial; the command never retries or rolls
back automatically. The boundary is same-UID protection, not an OS sandbox.

Supported operations are `stash-paths`, `ff-only`, and `apply-index`. Every
request has `version`, `operation`, `repo`, `expected_head`, `expected_remote`,
`snapshot_sha256`, `evidence`, `expires_at`, and `session`. Stash requests add
nonempty tracked `paths`; fast-forward requests add `target`; index-apply
requests add the exact retained `stash` SHA. For example:

```json
{"version":1,"operation":"stash-paths","repo":"/absolute/repo","expected_head":"<40-hex>","expected_remote":"<40-hex>","snapshot_sha256":"<64-hex>","evidence":[],"expires_at":1234567890,"session":"label","paths":["tracked.txt"]}
```

Input is strict JSON only. The command prints a JSON status: invalid input
exits 2 with `rejected`; a declined dialog reports `cancelled`; successful
execution reports `success`; and `failed-or-partial` must be investigated
without retrying automatically.
