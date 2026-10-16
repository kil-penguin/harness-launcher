# Explicit UI answer workflows

The CLI supports the Web UI answer workflow only when the selected device's
identity explicitly advertises `web_guarded_ui_answer_v1`: version 1, exactly
`web.ui_answer.prepare.v1`, `web.ui_answer.step.v1`, `web.ui_answer.get.v1`, and
`web.ui_answer.cancel.v1`, and agent `claude`. Missing or malformed advertisement
fails closed. Native capability validation remains the Web adapter's responsibility.

```bash
harness-herdr-web cli ui-answer-prepare --device <full-lowercase-uuid> --target-file <absolute-private-file> --prompt-file <absolute-private-file> --answer-file <absolute-private-file> --new-workflow
harness-herdr-web cli ui-answer-step --intent <intent-uuid> --expected-step 0 --expected-revision 0
harness-herdr-web cli ui-answer-status --intent <intent-uuid>
harness-herdr-web cli ui-answer-cancel --intent <intent-uuid>
```

All three input files must be caller-owned regular single-link files, mode 0600,
in a mode-0700 directory, with no symlink in their paths. Each is read once.
`target-file` is the unchanged `guard-target` output, including the full target
and matching device UUID. `prompt-file` is exactly
`{"pane_id":"pane-id","prompt_id":"abcdef012345"}`; replace that illustrative
prompt ID with the currently observed twelve lowercase hexadecimal digits.
`answer-file` is exactly `{"option_index":0}`, an ordinary option index 0..32.
No keys, custom text, multiple selections, provider controls, ticket, session,
or caller-derived plan are accepted. The selected snapshot epoch must be non-null
and match the target. UUIDs must be full canonical lowercase values.

Every prepare creates a new local intent and sends at most one POST. Every step
requires explicit canonical decimal `--expected-step` (0..32) and
`--expected-revision` (0..18446744073709551615). Use counters from a known result;
the CLI never infers them after a lost reply and never loops through later steps.
Status uses GET without query or body. All requests use the trusted discovered
loopback endpoint, a private token, a ten-second absolute deadline and a 65536-byte
request/response bound; redirects, proxy environments and arbitrary URLs are unused.

A durable private reservation and flushed start JSONL precede each prepare/step
POST. Start JSONL contains only `intent_uuid` and the Web method. Result JSONL
contains `intent_uuid` and the closed safe workflow, including actual Native
operation effects when known. No target, prompt, answer, binding, path, ticket,
token or raw upstream error is exposed. Full u64 numeric values remain exact.

Receipts live separately from guarded intents under
`~/.local/state/harness-herdr-web/ui-answer-receipts/v1`, with private owner-only
files, directory-FD access, generation CAS, atomic replacement and file/parent
fsync. A step reservation also creates a durable private no-replay marker. Only
the original successful ready-result CAS, after its file and directory fsync,
may unlink the matching marker. An unsafe marker or failed unlink retains the
ban; a crash may conservatively restore a deleted marker.

A lost/malformed step reply, pending crash record, result-persistence failure or
non-ready step result leaves the intent permanently get/cancel-only. A later GET
reporting ready or completed does not restore step admission. A workflow/epoch
mismatch retains the original locator and permits GET recovery only. A lost prepare
without a learned workflow UUID yields local `workflow_unknown` with no network
request for status/cancel. Cancel is sent once after its durable marker; after
cancel loss, recovery is GET only. No remaining step or POST is replayed.

`completed_ui_sequence` means the Web-derived PTY steps have known completed
Native results. It provides no provider acknowledgement or card-consumption
receipt. The Web screen-to-write residual remains: the observed menu can change
between its reread and an individual PTY write. Recovery refers only to the same
known workflow and Native operation, never a new answer or provider transaction.
