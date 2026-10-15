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

An opt-in request may additionally include `ssh`. It is exactly
`{"hostname":"github.com","user":"git","identity_file":"/absolute/private-key"}`.
This form supports only a `git@alias:owner/repository.git` origin. The launcher
uses the supplied existing private identity only for its live remote query,
with user SSH configuration and agents disabled; preservation operations keep
their existing Git argv. The identity must be a safe, owner-only regular file
outside the repository, Git common directory, and review evidence. Its path
and non-content metadata are shown in the one-shot confirmation and rechecked
after confirmation and immediately before mutation. Private-key contents are
never read or hashed by Python.

Input is strict JSON only. The command prints a JSON status: invalid input
exits 2 with `rejected`; a declined dialog reports `cancelled`; successful
execution reports `success`; and `failed-or-partial` must be investigated
without retrying automatically.
