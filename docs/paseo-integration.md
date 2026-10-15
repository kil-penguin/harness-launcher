# Paseo integration

[Paseo](https://paseo.sh) is a daemon plus desktop, web, and mobile clients that start and supervise coding agents. `harness-launcher` stays the policy owner: profile selection, harness env, MCP, hooks, skills, and model presets come from the launcher, and Paseo only supplies the conversation and its own argv.

Verified with Paseo 0.10.1, Claude Code 2.1.284, and codex-cli 0.158.0 on macOS.

## Managed providers

`harness-paseo` (v0.35.0+) writes the Paseo provider entries from the profile registry, so they never drift from `harness-profile register`:

```sh
harness-paseo print                 # expected provider fields as JSON
harness-paseo sync --reload         # merge them into ~/.paseo/config.json, then reload the daemon
harness-paseo check [--profile P]   # OK/WARN/FAIL report; exit 1 on FAIL
```

For each installation it manages:

| Provider ID | Label | Command |
| --- | --- | --- |
| `harness-claude` | `Harness Claude` | `<bin>/harness-auto claude base --passthrough` |
| `harness-codex-<profile>` | `<profile> Codex` | `<bin>/harness-codex --profile <profile>` |

`<bin>` is the directory `harness-paseo` was invoked from (`$(brew --prefix)/bin` for Homebrew); pass `--bin-dir` to override it. Profile IDs are lowercased with `_` mapped to `-`; profiles that collide on one ID are reported as `FAIL` and skipped.

Ownership rules:

- Sync edits only the `extends`, `label`, and `command` fields of the IDs it wrote, recorded in `~/.config/harness-launcher/paseo-managed.json`. Other provider keys (`enabled`, `models`, `env`, …) and every other config key are preserved.
- IDs that sync wrote earlier and no longer expects are removed, also from `agents.metadataGeneration.providers`, except an owned ID that now collides. Hand-written `harness-*` entries it does not own are left alone with a `WARN`; a hand-written entry with an expected ID is adopted with a `WARN`.
- The config must already exist (`FAIL: start Paseo once`); sync never creates it, because a new file without `daemon.relay.enabled: false` would enable the relay under Paseo's compatibility default.
- Nothing is written when nothing changes. Otherwise sync replaces the config atomically with its mode kept (through a symlink to its target) after writing a 0600 backup `config.json.harness-paseo.bak`, and aborts with exit 1, writing neither, if Paseo rewrote the file meanwhile.
- `check` also fails when `<bin>/harness-auto` or `<bin>/harness-codex` is missing, and warns when the relay is not disabled or when `<bin>` resolves into a Git checkout (the provider would change with every branch switch).

A minimal config after sync:

```json
{
  "daemon": { "listen": "127.0.0.1:6767", "relay": { "enabled": false } },
  "agents": {
    "providers": {
      "harness-claude": {
        "extends": "claude",
        "label": "Harness Claude",
        "command": ["/opt/homebrew/bin/harness-auto", "claude", "base", "--passthrough"]
      },
      "harness-codex-alpha": {
        "extends": "codex",
        "label": "alpha Codex",
        "command": ["/opt/homebrew/bin/harness-codex", "--profile", "alpha"]
      }
    }
  }
}
```

## Claude

Paseo runs Claude Code through the Claude Agent SDK with `settingSources: user, project, local` and the `claude_code` system prompt preset, so `CLAUDE.md`, project settings, and hooks load from the agent's working directory. The provider command is an argv prefix; Paseo appends the SDK arguments (`--output-format stream-json --input-format stream-json`, `--permission-mode`, `--effort`, `--model`, `--mcp-config`, …) and talks over piped stdio.

`--passthrough` (v0.34.0+) ends launcher keyword parsing. Everything after it reaches Claude verbatim, and an explicit caller `--model`, `--effort`, or `--permission-mode` replaces the launcher default. Since v0.35.0:

- A Claude management subcommand right after the marker (Paseo's `auth status` diagnostic) runs natively without launcher flags.
- An explicit caller thinking disable (`--thinking disabled`, `--max-thinking-tokens 0`, or inline `--settings` with `"alwaysThinkingEnabled": false`) drops a launcher `xhigh`/`max` effort, which requires thinking. Other `--settings` values, such as Paseo's `fastMode`, keep it.
- A caller `--` ends option scanning: later tokens are prompt text.

`harness-auto` selects the registered profile that owns the agent's working directory, so one provider serves every registered harness.

## Codex

Paseo starts one `<command> app-server [--enable goals]` per agent with no working directory of its own and sends each thread's `cwd` over JSON-RPC. It probes `<command[0]> --version` for feature gates. `harness-codex` covers both:

- `harness-codex --version` runs native `codex --version`.
- `harness-codex --profile <p> app-server …` runs `harness-profile <p> codex --passthrough app-server …`. The profile, not the daemon's working directory, selects the harness, so there is one provider per registered profile.
- Without `--profile`, the current directory selects the harness through `harness-auto`.

For a plain `app-server`, the launcher omits `-p` (Codex rejects it there) and starts the server behind `codex-app-server-guard.py`, a JSON-RPC relay that keeps execution roots inside the selected harness:

- Every client message is scanned for `cwd`, `cwds`, `runtimeWorkspaceRoots`, `extraRoots`, and `selectedCapabilityRoots[].location.path`. A value must be an absolute path whose real path is the harness root or below it and not inside another registered harness nested in it. A request that violates this gets a JSON-RPC error (`-32602`, "outside the `<prefix>` harness boundary") instead of reaching Codex; a violating notification is dropped.
- `thread/resume` and `thread/fork` without `cwd` get the server's working directory, so a thread recorded elsewhere cannot resume outside the harness.
- Not inspected, by design: explicit file operations (`fs/*`), config writes, plugin and project records, rollout paths, permission grants, fuzzy-search roots, `sandboxPolicy.writableRoots`, turn attachments (Paseo sends images as temp-file paths), and free-form `config` overrides. The guard is a profile-consistency boundary for a trusted local client, not a sandbox.
- `app-server daemon`/`proxy`, `--listen` values other than `stdio://`, `remote-control`, `exec-server`, and `mcp-server` exit 2 because they would serve clients around the relay. Use `command codex` for them deliberately.
- Malformed messages (unparseable lines, batches, duplicate keys, paths that cannot be checked) get a JSON-RPC error and are not forwarded; the relay keeps running.

In a Paseo workspace outside the profile's harness, thread start fails with the boundary error; use the provider of the harness that owns the workspace. The app-server runs without `-p`, so it uses the top-level `config.toml` defaults of the profile's `CODEX_HOME`, and Paseo selects the model per thread.

Other Codex callers get the same rules through `--passthrough`: a caller `-p`/`--profile` replaces the launcher profile, and a caller `-C`/`--cd` (any form) replaces the launcher `--cd` after it is validated to stay inside the harness.

## Workspaces

- Use Paseo `local` workspaces whose path is a registered harness root or a directory inside it, such as `<harness>/projects/<repo>`.
- Paseo-managed worktrees live under the global `worktrees.root` with an internal per-repository hash, outside every registered harness, and the launcher fails closed there. Create the worktree inside the owning harness and add it as a local workspace:

  ```sh
  git -C <harness>/projects/<repo> worktree add -b <branch> .worktrees/<slug> origin/HEAD
  paseo workspace create --isolation local --path <harness>/projects/<repo>/.worktrees/<slug> --title <slug>
  # when done
  paseo workspace archive <workspace-id>
  git -C <harness>/projects/<repo> worktree remove .worktrees/<slug>
  ```

- Fresh launches without a TTY never take the isolated-session route, so an isolation-default profile runs in its canonical root, as with any other non-interactive launch. A non-TTY Claude continuation (`--resume`, `--continue`, `--fork-session`) is owner-checked when isolation is on and refused when it belongs to the canonical checkout; a host that passes an option such as `--verbose` before `--resume` is classified as an invalid argv and still runs in the canonical root (the harness write guard covers its writes).

## Environment hygiene

- Start the daemon from a fresh login shell or through the desktop app, never from inside an agent session. A daemon inherits its parent environment and passes it to every agent; Paseo strips only `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`, and `CLAUDE_AGENT_SDK_VERSION`, so tokens and a non-Orca `CODEX_HOME` from the parent would leak. The runtime variables below no longer do (v0.36.0+): a launcher-run agent under an SDK host has no TTY, counts as runtime `plain`, and has `CMUX_*`, `ORCA_*`, `HERDR_*` and `TERM_PROGRAM=Orca` removed (see [Terminal runtimes](terminal-runtimes.md#detection)). Starting the desktop app at login keeps the daemon available after a reboot.
- Do not set `HARNESS_SESSION_ISOLATION=1` in the daemon environment. It forces isolation routing, which rejects SDK options such as `--verbose` and `--mcp-config`.
- Relay is off on new Paseo homes. Enable it only when you need mobile access; its traffic is end-to-end encrypted.

## Known limits

- An explicitly isolated `app-server` (`--isolated codex app-server`) fails closed: the isolated session root does not contain the canonical working directory. SDK hosts launch without a TTY and do not start isolated sessions.
- With the `kiro` or `codex-gateway` provider prefixes, a full model ID from Paseo is passed as-is and is not mapped through `ANTHROPIC_DEFAULT_*`.
- With a caller `--settings` (Paseo's `fastMode`), the launcher's forced-thinking `--settings` for `xhigh`/`max` is passed as a second `--settings` ahead of it.
