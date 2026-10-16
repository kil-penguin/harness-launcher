# Bounded pane read CLI (unadvertised)

```sh
harness-herdr-web cli read --device FULL_UUID --pane PANE_ID
harness-herdr-web cli read --device FULL_UUID --pane PANE_ID --body
harness-herdr-web cli read --device FULL_UUID --pane PANE_ID --body --lines 40 --max-bytes 4096
```

The default returns only the selected existing pane metadata row:
`workspace_id`, `tab_id`, `pane_id`, `terminal_id`, `agent`, `agent_status`.
It makes no body request. Device selection requires a full lowercase UUID,
including for this computer. There are no URL, session, source, format or ANSI
rendering options.

`--body` explicitly requests current visible terminal text once. `--lines` and
`--max-bytes` require `--body`; defaults are 40 lines / 4096 UTF-8 bytes, and
maxima are 200 / 8192. Both must be canonical positive unsigned decimal strings.
The command checks options before network access, reads identity and the selected
snapshot, and requires one pane membership and a non-null epoch before body read.
It sends that exact workspace/tab/pane/terminal tuple and epoch. A stale target
fails closed without selecting a replacement, retrying, or using another source.

Body success is one closed JSON object with `device_id`, `server_epoch`, the four
resource IDs, `text` and boolean `truncated`. UTF-8 text must fit the requested
byte cap. Terminal controls are JSON-escaped; parse JSON to obtain the original
text. The CLI does not render it, log it, persist it, or expose private Native
targets, proofs, sessions or raw upstream errors. JSON escaping may expand the
serialized output beyond the text byte cap; transport frames are capped at 65536.

Identity, snapshot and body requests share one absolute 10-second command budget;
owned sockets close on timeout, EOF or Ctrl-C. Local discovery/authentication and
remote drive/origin checks remain unchanged. HTTP 404 means unsupported. Native
read errors use only the three fixed reviewed pairs; other errors use closed
transport mappings. Read-only or revoked access never returns a later body.

This consumer remains unadvertised until independent implementation/security and
cross-repository integration reviews pass. It grants no ownership, interrupt,
close or provider acknowledgement authority. Web and Native own fresh capability,
current target comparison, role rechecks, UTF-8 truncation and full RPC envelope
bounds. This CLI does not assert an internal viewport allocation limit.
