# Optional native response collection

A profile may opt in with `config/ssot-session-hooks.json`:

```json
{"enabled": true}
```

Only this exact object with a boolean true enables the integration. Missing,
malformed or unknown-key files disable it; process environment is ignored.
Other runtime integrations keep their existing launcher.env opt-ins. In isolated
sessions the opt-in comes from that session snapshot; the launch record still
binds to the canonical source root even when its checkout is older.

Claude launch settings and generated Codex hooks append UserPromptSubmit and
Stop callbacks after existing hooks, plus SubagentStart (for Codex only when the
launch record hook is also on). They invoke the optional host callback
`$HOME/.local/share/harness-service/bin/harness-session-hook` with
`--runtime claude` or `--runtime codex`. Both callbacks discard output and errors
and always exit successfully, including when the host callback is absent.
They never block continuation or change permissions. Codex's normal hook trust
still applies; changed opt-ins invalidate the warm home fingerprint.

The host callback owns collection policy, private durable delivery and
authentication. It must bind the native session to the intended profile using
the launch record; the launcher does not forward transcripts or credentials.
The callback path is intentionally optional, so a host without the service
does not gain a server connection. Enable only in the profile and host where
session collection is intended. Native MCP registration is separate and uses
the profile's existing exact server definitions.

Remove the JSON file or set enabled to false to disable future callbacks, then
regenerate the Codex home and refresh native hook trust. Already running,
pinned sessions retain their original hooks. No hook is added to other profiles
by installing this launcher release.

## Automatic session linking (P3-L, launcher side)

The same opt-in also links each native session to a common Task. The launcher
only produces facts; the host callback decides what to capture and send.

- `SessionStart` is the callback's session-start event. When the profile also
  has the launch record hook (`HARNESS_LAUNCH_RECORD_HOOKS=1` for Codex, always
  for Claude), there is exactly ONE `SessionStart` command: the record helper
  writes `<state>/launch-records/<agent>-<session_id>` durably (file and
  directory fsync, atomic replace) and then runs
  `harness-session-hook --runtime <agent>` with the same payload
  (`harness-launch-record <agent> ... --callback`). No hook ordering is assumed
  and no second `SessionStart` row is written next to it. Without the launch
  record hook (Codex) the ssot row adds neither `SessionStart` nor
  `SubagentStart`: capture needs the record and would drop them.
- `SubagentStart` runs the callback only (no record step); on Codex it is emitted
  only together with the launch record hook.
- Timeouts: the combined `SessionStart` command is 10 seconds (it was 5 for the
  record alone), because the record helper keeps its own 5 second limit for the
  single process-table read and the callback gets what is left (it is cut off
  one second before the hook limit). `SubagentStart` stays 5. Profiles without
  the ssot opt-in keep the old 5 second record-only row. The hook prints
  nothing and exits 0 even when the callback is missing, slow or failing.
- The record gains `source` (the `SessionStart` source), `agent_pid` and
  `agent_start` (the agent process found by the ancestry walk and its `ps`
  start time in UTC with single spaces; they are omitted when the process table cannot
  be read), `task_id` and `parent_native`. The helper still reads the process
  table once (`ps -axo pid=,ppid=,lstart=,comm=`, 5 second limit); an unreadable
  table still counts as nested and withholds the grant. The collector derives the
  process key from `agent_pid` and `agent_start`; it does not run `ps` again.
- `--task <uuid>` (launcher keyword, before `--passthrough`) names the Task for
  this session. Claude receives it as the hook argument `--task-id`, Codex as
  `HARNESS_LAUNCH_TASK_ID`; neither is read from settings `env`. A fork of an
  explicit parent (`--fork-session` with `--resume <uuid>` for Claude, `fork
  <uuid>` after `--passthrough` for Codex) records `parent_native`.
- `harness-headless --journal-board <board> --journal-task <t_xxxxxxxx>`
  (together, or neither) runs the callback once with `--runtime buzz` and a
  `RunStart` payload of ids only. Without both arguments nothing is sent; the
  collector additionally drops runtimes that are not enabled.

Hook approval grant: for an ssot profile whose Codex row is the combined
`SessionStart` command (`... harness-launch-record codex --callback`), the
hook-approval grant used to carry approved hooks into isolated homes must hold a
`callbacks` entry for `$HOME/.local/share/harness-service/bin/harness-session-hook`
with that file's sha256, next to the launcher callbacks. Without the entry, or when
the file's bytes differ from it, the row is not carried and native review remains.
Rows without `--callback` need no such entry. In carried isolated Codex sessions
only `SessionStart` runs the host callback; `SubagentStart`, `UserPromptSubmit` and
`Stop` still do not run there (their host-script rows are not carried).

Hook trust: changed Codex hook commands and timeouts need a new Codex hook trust
approval per profile (see above). Claude hooks have no trust step.
