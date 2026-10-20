# Contributing to harness-launcher

Thanks for taking the time to improve the project. This repository is small, but launcher changes sit on top of shell startup, credentials, MCP configuration, and multiple AI CLIs. Focused pull requests with clear tests are much easier to review than broad rewrites.

## Before opening a change

Use an issue when you want to:

- add a runtime, provider, or new public command shape;
- change a default model, effort, sandbox, or approval policy;
- change how auth, MCP servers, plugins, hooks, or skills are discovered;
- alter generated files under `.harness/`;
- make a breaking change to `config/launcher.env`.

Small bug fixes and documentation corrections can go straight to a pull request.

Security reports do not belong in public issues. Follow [SECURITY.md](SECURITY.md).

## Development setup

Requirements:

- macOS
- Git
- Zsh
- Bash
- Python 3

Clone your fork and create a branch:

```bash
git clone https://github.com/<your-user>/harness-launcher.git
cd harness-launcher
git checkout -b fix/short-description
```

You can test the source tree without installing it system-wide:

```zsh
source "$PWD/bin/aliases.zsh"
harness_register "/path/to/a/test-harness"
```

Use a disposable project directory for manual tests. Do not point experimental launcher code at a project containing credentials or irreplaceable generated state.

## Project structure

```text
bin/                       launcher and runtime adapters
  aliases.zsh              registration, shortcuts, completion, wrappers
  launcher.sh              interactive TUI
  codex-home-prepare.sh     generated per-project Codex home
  kiro-home-prepare.sh      generated per-project Kiro home
test/                      shell regression tests
 docs/                      architecture and runtime documentation
 install.sh                 disabled source-install migration stub
```

Installed Homebrew files and `<project>/.harness/**` are generated outputs. Change the canonical files in this repository instead.

## Tests

Run every test through its declared interpreter:

```bash
./test/run-all.sh
```

The no-argument command remains the full ordered suite. The CI partition uses
the same ordered inventory, including the session-creation lease and recovery
regressions; existing interpreter, source admission,
conditional-skip and first-error behavior are unchanged.

CI uses the same runner in four fixed groups: `headless`, `shell`,
`preservation` and `python`. Each runs sequentially within its own isolated
`macos-15` job with a 20-minute limit. Each job retains dependency installation,
owned Homebrew preparation, source-bound jq proof, shell syntax and docs checks.
The required `test` aggregate needs all four jobs to return `success` and
verifies their current-run/source inventory coverage; failed, cancelled,
skipped, missing or partial results cannot pass. New default tests must remain
covered exactly once. See [CI regression
partition](docs/architecture.md#ci-regression-partition) for the CI-only
interfaces. This does not replace the full local command above.

`test-codex-home-prepare.sh` exercises the production `/usr/bin/lockf` contract. The runner reports it as skipped only on hosts where that macOS system binary is unavailable; run it on a supported Mac before merging changes to shared Codex cache or browser-plugin behavior.

Run syntax checks before opening a pull request:

```bash
bash -n bin/*.sh test/*.sh
zsh -n bin/aliases.zsh bin/*.sh test/*.sh
```

For behavior changes, use a RED/GREEN workflow:

1. Add a regression test that fails on `main`.
2. Implement the smallest fix.
3. Run the targeted test.
4. Run the full suite.

Tests should use temporary homes, fake runtimes, and fake project directories. They must not depend on the contributor's real `~/.codex`, auth files, MCP servers, or project repositories.

## Shell and portability rules

- The supported platform is macOS.
- Keep Zsh-specific behavior inside Zsh entry points and tests.
- Do not assume GNU variants of `sed`, `stat`, `readlink`, or `date`.
- Quote paths. Project directories and app bundles can contain spaces.
- Avoid `eval` for user-controlled values. `HARNESS_PREFIX` currently creates a function name, so changes around registration require extra scrutiny and tests.
- Prefer arrays over string-built commands.
- Preserve exit codes from the selected runtime.
- Do not print environment values, auth material, bearer tokens, or full local configuration.

## Runtime-home and security invariants

Changes must preserve these boundaries:

- Runtime homes stay under `<project>/.harness/<runtime>`. Native history archives
  and source-bound metadata live in `<state>/native-history`, outside disposable
  Git roots; auth, generated configuration and grants are excluded. Another
  exception is `bin/claude-plugin-scope-mirror.py`, which edits Claude Code's user
  plugin registry for isolated session roots; preserve its ownership and locking
  boundaries.
- Generated runtime homes are not the durable source for user-authored config or skills.
- Native Codex auth is referenced through the selected auth file; do not copy or transform refresh tokens.
- Shared global Codex cache writes remain serialized with macOS `lockf`.
- Browser client trust uses exact SHA-256 allowlisting. Do not add project-writable directories to `NODE_REPL_TRUSTED_CODE_PATHS`.
- Local MCP and gateway secrets stay outside committed files.
- Duplicate MCP names fail instead of silently overriding committed configuration.
- The terminal runtime from `PATH` remains preferred over a potentially older app-bundled CLI unless the user explicitly opts in.

Read [docs/architecture.md](docs/architecture.md) and [SECURITY.md](SECURITY.md) before touching these areas.

## Documentation changes

Keep public examples generic. Do not include:

- personal home directories;
- company or client names;
- internal service URLs;
- real account labels;
- screenshots containing local paths or credentials.

When a command, environment variable, generated file, or compatibility rule changes, update the README and the relevant document in `docs/` in the same pull request.

## Commit and pull request guidance

Use a short, descriptive commit message. Conventional Commit prefixes are welcome but not required:

```text
fix: preserve lock ownership across signals
docs: explain local MCP overlays
test: cover prefixes containing hyphens
```

A pull request should explain:

- the problem;
- the chosen behavior;
- compatibility or security implications;
- tests that were run;
- manual verification, when applicable.

Keep generated files, credentials, editor state, and unrelated formatting changes out of the diff.

## Pull request checklist

- [ ] The change is scoped to one problem.
- [ ] New behavior has a regression test.
- [ ] `./test/run-all.sh` passes.
- [ ] Bash and Zsh syntax checks pass.
- [ ] Public docs contain no secrets or private identifiers.
- [ ] README/docs match the implemented command and config behavior.
- [ ] Existing runtime-home and trust boundaries are preserved.
- [ ] Breaking changes include a migration note.

## Maintainer release flow

Maintainers publish from a clean `main` branch:

1. Merge a reviewed pull request.
2. Tag the source release.
3. Publish GitHub release notes.
4. Update `kilhyeonjun/homebrew-tap` to the new tag.
5. Upgrade and smoke-test the Homebrew package on a clean shell.
6. Verify registered project repositories gained no new tracked changes.

Do not patch `/opt/homebrew/share/harness-launcher` as the long-term source. Emergency installed-file changes must be promoted back into this repository before the next Homebrew upgrade.
