/** W07 required bridge. No native/package imports. Only compiler emits copies.
 * Hashes are integrity evidence; existing launcher admission remains authority.
 */
import { createHash } from 'node:crypto';
import { readFileSync, lstatSync, realpathSync, accessSync, constants } from 'node:fs';
import { dirname, resolve, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

type RecordValue = Record<string, any>;
type NativeToolResult = { toolName: string; toolCallId: string; input: RecordValue;
  content: ({ type: 'text'; text: string } | { type: 'image'; data: string; mimeType: string })[];
  details?: unknown; isError: boolean };
type Context = { cwd: string; sessionManager: { getSessionId(): string }; abort(): void; shutdown(): void };
type API = { on(event: string, handler: (event: RecordValue, context: Context) => any): void;
  appendEntry(type: string, value: unknown): void;
  requiredLoadStatus?(): Readonly<RecordValue> };
export type Services = {
  load(): RecordValue;
  current(manifest: RecordValue, context: Context): Promise<boolean>;
  run(hook: RecordValue, payload: RecordValue, signal: AbortSignal): Promise<{ code: number; stdout: string }>;
  transforms?: Record<string, (raw: RecordValue, signal: AbortSignal) => Promise<RecordValue>>;
};
const digest = (b: string | Buffer) => createHash('sha256').update(b).digest('hex');
const canonical = (value: any): string => JSON.stringify(sort(value));
function sort(value: any): any {
  if (Array.isArray(value)) return value.map(sort);
  if (object(value)) return Object.fromEntries(Object.keys(value).sort().map(k => [k, sort(value[k])]));
  return value;
}
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value));
function freeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze); Object.freeze(value);
  }
  return value;
}
function requireValue(ok: unknown, reason: string): asserts ok {
  if (!ok) throw new Error(`OMP_REQUIRED_${reason}`);
}
function object(value: unknown): value is RecordValue {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
export function admitComposition(extensions: RecordValue[]) {
  // Called by owned normal prepare before extension loading as well as here.
  for (const e of extensions) requireValue(e.native_input_behavior === 'none' &&
    /^[a-f0-9]{64}$/.test(e.closure_sha256) && !!e.canonical_source_evidence, 'UNSUPPORTED_INPUT_WRITER');
}
function semantic(tool: string, raw: RecordValue): { name: string; input: RecordValue } {
  if (tool === 'bash') {
    requireValue(typeof raw.command === 'string', 'BASH_INPUT');
    requireValue(Object.keys(raw).every(k => ['command', 'timeout', 'path', 'paths'].includes(k)), 'UNSUPPORTED_BASH_SHAPE');
    requireValue(raw.timeout === undefined || (Number.isFinite(raw.timeout) && raw.timeout > 0), 'BASH_TIMEOUT');
    return { name: 'Bash', input: { ...raw } };
  }
  if (tool === 'write') {
    requireValue(typeof raw.path === 'string' && typeof raw.content === 'string', 'WRITE_INPUT');
    return { name: 'Write', input: { ...raw, file_path: raw.path } };
  }
  if (tool === 'read') {
    requireValue(typeof raw.path === 'string', 'READ_INPUT');
    return { name: 'Read', input: { ...raw, file_path: raw.path } };
  }
  // Native edit/patched/computer/python raw shapes need their exact source
  // adapter. Refuse that action rather than guessing Claude's input fields.
  throw new Error('OMP_REQUIRED_UNSUPPORTED_TOOL_MAPPING');
}
function rawInput(tool: string, normalized: RecordValue): RecordValue {
  requireValue(tool === 'bash', 'UNSUPPORTED_RAW_REWRITE');
  // Bash command is the only currently proven lossless rewrite adapter.
  requireValue(typeof normalized.command === 'string', 'RAW_INPUT');
  const raw: RecordValue = {};
  for (const k of ['command', 'timeout']) if (k in normalized) raw[k] = normalized[k];
  requireValue(Object.keys(normalized).every(k => ['command', 'timeout', 'path', 'paths'].includes(k)), 'RAW_UNKNOWN_FIELD');
  return raw;
}
function nativeRelease(release: RecordValue, version: string) {
  const fields = ['contract_id', 'upstream_base_version', 'upstream_base_revision', 'version',
    'source_revision', 'source_patch_sha256', 'build_provenance_sha256', 'binary_sha256'];
  requireValue(object(release) && Object.keys(release).length === fields.length && fields.every(k => k in release) &&
    release.contract_id === 'w07.required-resolver-consumer.v1' && release.version === version &&
    version === '18.11.3-kh.w07.1' && release.upstream_base_version === '18.11.3' &&
    release.upstream_base_revision === '96894a6147d8cc64b7e00ad3a7d7178fd93b9531' &&
    /^[a-f0-9]{40}$/.test(release.source_revision) &&
    ['source_patch_sha256', 'build_provenance_sha256', 'binary_sha256'].every(k =>
      typeof release[k] === 'string' && /^[a-f0-9]{64}$/.test(release[k])), 'NATIVE_RELEASE_BINDING');
}
export function registerRequired(api: API, services: Services) {
  let manifest: RecordValue | undefined, ready = false, session = '', closed = false;
  let nativeEpoch: number | undefined, starting = false;
  let startupContext: string[] = [], promptContext: string[] = [], pendingPrompt: string | undefined;
  const observedResults = new Set<string>();
  const deny = () => ({ block: true, reason: 'OMP required policy admission failed' });
  function clearContext() { startupContext = []; promptContext = []; pendingPrompt = undefined; }
  function halt(ctx: Context) { ready = false; clearContext(); ctx.abort(); ctx.shutdown(); }
  function nativeStatus(ctx: Context) {
    if (manifest?.schema !== 2) return;
    requireValue(typeof api.requiredLoadStatus === 'function', 'NATIVE_STATUS_MISSING');
    const value = api.requiredLoadStatus();
    requireValue(value && value.contract_id === 'w07.required-resolver-consumer.v1' &&
      value.selection_sha256 === manifest.effective_selection_sha256 &&
      value.manifest_sha256 === manifest.manifest_sha256 &&
      value.session_id === session && value.session_id === ctx.sessionManager.getSessionId() &&
      Number.isSafeInteger(value.epoch) && value.epoch >= 0 &&
      value.phase === (starting ? 'consumed_wait_policy' : 'ready'), 'NATIVE_CONSUMED_BINDING');
    if (nativeEpoch === undefined) nativeEpoch = value.epoch;
    requireValue(value.epoch === nativeEpoch, 'NATIVE_EPOCH');
  }
  async function current(ctx: Context) {
    requireValue(manifest && !closed && ctx.sessionManager.getSessionId() === session, 'SESSION_BINDING');
    try {
      nativeStatus(ctx);
      requireValue(await services.current(manifest, ctx), 'REVOKED_OR_FOREIGN');
      nativeStatus(ctx);
    } catch (error) { ready = false; clearContext(); throw error; }
  }
  async function deadline<T>(work: (signal: AbortSignal) => Promise<T>, milliseconds: number, parent?: AbortSignal): Promise<T> {
    const ctl = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    let rejectParent: (error: Error) => void;
    const cancelled = new Promise<never>((_, reject) => { rejectParent = reject; });
    const cancel = () => { ctl.abort(); rejectParent(new Error('OMP_REQUIRED_ABORTED')); };
    if (parent?.aborted) throw new Error('OMP_REQUIRED_ABORTED');
    parent?.addEventListener('abort', cancel, { once: true });
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { ctl.abort(); reject(new Error('OMP_REQUIRED_TIMEOUT')); }, milliseconds);
    });
    try { return await Promise.race([work(ctl.signal), timeout, cancelled]); }
    finally { clearTimeout(timer!); ctl.abort(); parent?.removeEventListener('abort', cancel); }
  }
  function payload(event: RecordValue, ctx: Context, kind: string, raw?: RecordValue) {
    const view = raw ? semantic(event.toolName, raw) : undefined;
    return { hook_event_name: kind, session_id: session, tool_use_id: event.toolCallId,
      cwd: ctx.cwd, tool_name: view?.name, tool_input: view?.input,
      is_error: event.isError, prompt: event.text };
  }
  async function hooks(kind: string, p: RecordValue, signal: AbortSignal, final = false) {
    const context: string[] = [];
    for (const hook of manifest!.hook_map.filter((h: RecordValue) => h.event === kind &&
      (!p.tool_name || new RegExp(`^(?:${h.matcher})$`).test(p.tool_name)))) {
      requireValue(!final || hook.decision_only, 'UNSUPPORTED_EFFECTFUL_RECHECK');
      requireValue(!signal.aborted, 'ABORTED');
      const result = await deadline(s => services.run(hook, p, s),
        Math.min(hook.timeout_ms, Math.max(1, manifest!.bridge_total_timeout_ms)), signal);
      requireValue(result.code === 0, 'HOOK_NONZERO');
      const output = result.stdout.trim();
      if (!output) continue;
      const parsed = JSON.parse(output);
      requireValue(object(parsed), 'HOOK_MALFORMED');
      const decision = parsed.hookSpecificOutput ?? parsed;
      requireValue(!('input' in parsed) && !('updatedInput' in decision), 'DECISION_REWRITE');
      requireValue(parsed.decision !== 'block' && parsed.continue !== false &&
        decision.permissionDecision !== 'deny' && !parsed.error, 'CANONICAL_DENY');
      if (decision.additionalContext !== undefined) {
        requireValue(typeof decision.additionalContext === 'string', 'CONTEXT_SHAPE');
        context.push(decision.additionalContext);
      }
    }
    return context;
  }
  // First registration is default-deny; no reads, imports or asynchronous setup above it.
  api.on('tool_call', async (event, ctx) => {
    if (!ready) return deny();
    try {
      return await deadline(async signal => {
        await current(ctx);
        requireValue(typeof event.toolCallId === 'string' && !!event.toolCallId && object(event.input), 'CALL_ID');
        const original = copy(event.input);
        const originalPayload = payload(event, ctx, 'PreToolUse', original);
        const context = await hooks('PreToolUse', originalPayload, signal);
        const transforms = manifest!.bridge_rewrites.filter((t: RecordValue) => t.tool === event.toolName);
        const passive = () => context.length ? { additionalContext: [...new Set(context)].join('\n') } : {};
        if (!transforms.length) { await current(ctx); return passive(); }
        let raw = rawInput(event.toolName, original);
        for (const transform of transforms) {
          requireValue(!!services.transforms?.[transform.canonical_entry_id], 'REWRITE_MISSING');
          raw = copy(await deadline(s => services.transforms![transform.canonical_entry_id](freeze(copy(raw)), s),
            transform.timeout_ms ?? manifest!.bridge_total_timeout_ms, signal));
          requireValue(object(raw) && !signal.aborted, 'REWRITE_MALFORMED');
          // Validate RAW, never return normalized path/paths gate-only fields.
          requireValue(Object.keys(raw).every(k => ['command', 'timeout'].includes(k)), 'REWRITE_RAW_SHAPE');
          semantic(event.toolName, raw);
        }
        await current(ctx);
        context.push(...await hooks('PreToolUse', payload(event, ctx, 'PreToolUse', raw), signal, true));
        requireValue(!signal.aborted, 'ABORTED');
        await current(ctx);
        return { input: freeze(copy(raw)), ...passive() };
      }, manifest!.bridge_total_timeout_ms);
    } catch { return deny(); }
  });
  api.on('session_start', async (_event, ctx) => {
    ready = false; starting = true; nativeEpoch = undefined; clearContext(); observedResults.clear();
    try {
      manifest = freeze(copy(services.load()));
      requireValue(manifest.runtime === 'omp' &&
        ((manifest.schema === 1 && manifest.runtime_version === '18.11.3' && !manifest.contract_id && !manifest.native_release) ||
         (manifest.schema === 2 && manifest.contract_id === 'w07.required-resolver-consumer.v1' &&
          manifest.runtime_version === '18.11.3-kh.w07.1' &&
          manifest.native_release?.contract_id === manifest.contract_id &&
          manifest.native_release?.version === manifest.runtime_version &&
          /^[a-f0-9]{64}$/.test(manifest.manifest_sha256))), 'SCHEMA');
      if (manifest.schema === 2) nativeRelease(manifest.native_release, manifest.runtime_version);
      requireValue(Number.isInteger(manifest.bridge_total_timeout_ms) && manifest.bridge_total_timeout_ms > 0 &&
        manifest.bridge_total_timeout_ms < 30000, 'BUDGET');
      admitComposition(manifest.selected_extensions ?? []);
      session = ctx.sessionManager.getSessionId(); requireValue(!!session && !closed, 'IDENTITY');
      startupContext = await deadline(async signal => {
        await current(ctx); const context = await hooks('SessionStart', payload({}, ctx, 'SessionStart'), signal);
        await current(ctx); return context;
      },
        manifest.bridge_total_timeout_ms);
      ready = true;
      api.appendEntry('harness-omp-selection', manifest.schema === 2 ? {
        contract_id: manifest.contract_id, selection_sha256: manifest.effective_selection_sha256,
        manifest_sha256: manifest.manifest_sha256, session_sha256: digest(session), epoch: nativeEpoch,
        phase: 'consumed_wait_policy', count: manifest.selected_skills.length,
      } : { selection_sha256: manifest.effective_selection_sha256,
        profile: manifest.profile, session_id: session, skills: manifest.selected_skills.length,
        hooks: manifest.hook_map.length });
    } catch { halt(ctx); }
    finally { starting = false; }
  });
  api.on('input', async (event, ctx) => {
    pendingPrompt = undefined; promptContext = [];
    try {
      requireValue(ready && event.source === 'interactive' && typeof event.text === 'string', 'UNSUPPORTED_PROMPT_MODE');
      promptContext = await deadline(async signal => {
        await current(ctx); const context = await hooks('UserPromptSubmit', payload(event, ctx, 'UserPromptSubmit'), signal);
        await current(ctx); return context;
      },
        manifest!.bridge_total_timeout_ms);
      pendingPrompt = event.text;
      return {};
    } catch { halt(ctx); return { handled: true }; }
  });
  api.on('before_agent_start', async (event, ctx) => {
    try {
      requireValue(ready, 'NOT_READY'); await current(ctx);
      if (pendingPrompt === undefined && !startupContext.length) return {};
      requireValue(event.prompt === pendingPrompt && Array.isArray(event.systemPrompt) &&
        event.systemPrompt.every((v: unknown) => typeof v === 'string'), 'UNSUPPORTED_CONTEXT_REQUEST');
      // Pinned native BeforeAgentStartEventResult.systemPrompt is per-request
      // policy, unlike a CustomMessagePayload which would persist raw context.
      const context = [...new Set([...startupContext, ...promptContext])];
      clearContext();
      return context.length ? { systemPrompt: [...event.systemPrompt, ...context] } : {};
    } catch { halt(ctx); return {}; }
  });
  api.on('user_bash', async (event, ctx) => {
    try {
      requireValue(ready, 'NOT_READY');
      await deadline(async signal => {
        await current(ctx);
        await hooks('PreToolUse', payload({ toolName: 'bash' }, ctx, 'PreToolUse', { command: event.command }), signal);
        await current(ctx);
      }, manifest!.bridge_total_timeout_ms);
      return undefined;
    } catch {
      // Native emitUserEvent returns this explicit replacement. Throwing would
      // be logged and permit the native fallback to execute the command.
      return { result: { output: 'OMP required policy denied this command', exitCode: 2, cancelled: true, truncated: false } };
    }
  });
  api.on('user_python', async (_event, _ctx) => ({ result: {
    output: 'OMP_REQUIRED_UNSUPPORTED_USER_PYTHON_MAPPING', exitCode: 2, cancelled: true, truncated: false,
  } }));
  api.on('resources_discover', async (_event, ctx) => {
    try {
      // Resources are discovered while native builds the session, before its
      // session_start event. Static loading never opens the tool readiness latch.
      if (!manifest) manifest = freeze(copy(services.load()));
      // Schema2 native resolver alone produces skills. This callback supplies none.
      if (manifest.schema === 2) return {};
      requireValue(!closed && manifest.schema === 1 && manifest.runtime === 'omp', 'RESOURCE_SELECTION');
      admitComposition(manifest.selected_extensions ?? []);
      requireValue(await services.current(manifest, ctx), 'RESOURCE_REVOKED');
      return { skillPaths: manifest!.selected_skills.map((s: RecordValue) => resolve(manifest!.bundle_dir, s.path)) };
    } catch { halt(ctx); return { skillPaths: [] }; }
  });
  api.on('tool_result', async (event, ctx) => {
    try {
      requireValue(ready, 'NOT_READY'); await current(ctx);
      const result = event as NativeToolResult;
      requireValue(typeof result.toolCallId === 'string' && !!result.toolCallId && object(result.input) &&
        typeof result.isError === 'boolean' && Array.isArray(result.content) && result.content.every(c =>
          object(c) && ((c.type === 'text' && typeof c.text === 'string') ||
            (c.type === 'image' && typeof c.data === 'string' && typeof c.mimeType === 'string'))), 'POST_RESULT_SHAPE');
      requireValue(!observedResults.has(result.toolCallId), 'POST_RESULT_REPLAY');
      const kind = result.isError ? 'PostToolUseFailure' : 'PostToolUse';
      const p = payload(event, ctx, kind, result.input);
      p.tool_response = copy({ content: result.content, ...(result.details === undefined ? {} : { details: result.details }),
        isError: result.isError });
      observedResults.add(result.toolCallId);
      await deadline(s => hooks(kind, p, s), manifest!.bridge_total_timeout_ms);
      await current(ctx);
    } catch { halt(ctx); }
  });
  api.on('session_stop', async (event, ctx) => {
    try { await current(ctx); await deadline(s => hooks('Stop', payload(event, ctx, 'Stop'), s), manifest!.bridge_total_timeout_ms); return {}; }
    catch { return deny(); }
  });
  api.on('session_shutdown', async (event, ctx) => {
    ready = false; closed = true; clearContext(); observedResults.clear();
    try { if (manifest) await deadline(s => hooks('SessionEnd', payload(event, ctx, 'SessionEnd'), s), 1800); }
    catch { /* Past shutdown effects are not described as undone. */ }
  });
  return { isReady: () => ready };
}

function ownedRead(path: string) {
  requireValue(isAbsolute(path) && realpathSync(path) === path, 'PATH');
  for (let node = path; ; node = dirname(node)) {
    const s = lstatSync(node);
    requireValue(!s.isSymbolicLink() && (s.uid === process.getuid!() || s.uid === 0) && !(s.mode & 0o022), 'ACL');
    if (dirname(node) === node) break;
  }
  const s = lstatSync(path); requireValue(s.isFile() && s.nlink === 1, 'FILE');
  return readFileSync(path);
}
async function runScript(path: string, args: string[], input: string, signal: AbortSignal, cwd: string, home?: string, hookPath?: string, hookPython?: string) {
  return await new Promise<{ code: number; stdout: string }>((accept, reject) => {
    // No settings.env/global authority copy and no inherited Claude/Codex identity.
    const env: Record<string, string> = { HARNESS_HOOK_RUNTIME: 'omp' };
    for (const key of ['HOME', 'PATH', 'LANG', 'LC_ALL', 'TMPDIR']) if (process.env[key]) env[key] = process.env[key]!;
    if (home) env.HOME = home; // Exact existing projection home, never parent env expansion.
    if (hookPath) env.PATH = hookPath;
    if (hookPython) env.HARNESS_PYTHON = hookPython; // Existing helper override; child only, exact sealed interpreter.
    const child = spawn(path, args, { cwd, env, signal, detached: true, stdio: ['pipe', 'pipe', 'pipe'] });
    const cancelGroup = () => { if (child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch {} } };
    signal.addEventListener('abort', cancelGroup, { once: true });
    let stdout = '', size = 0;
    child.stdout.on('data', b => { size += b.length; if (size > 65536) { child.kill(); reject(new Error('OMP_REQUIRED_OUTPUT_CAP')); } else stdout += b; });
    child.stderr.resume(); // Never persist policy/prompt/auth-bearing child diagnostics.
    child.on('error', reject); child.on('close', code => {
      signal.removeEventListener('abort', cancelGroup); accept({ code: code ?? 1, stdout });
    });
    child.stdin.on('error', reject); child.stdin.end(input);
  });
}
export default function requiredExtension(api: API) {
  const bundleDir = dirname(fileURLToPath(import.meta.url));
  let loaded: RecordValue;
  let hookPath: string;
  let hookPython: string;
  function executionClosure(m: RecordValue) {
    const dependencies = m.hook_dependencies ?? [];
    if (!m.hook_map.length) return;
    requireValue(dependencies.length === 3 && ['bash', 'jq', 'python3'].every(name =>
      dependencies.filter((d: any) => d.name === name).length === 1), 'HOOK_DEPENDENCIES_MISSING');
    requireValue(dependencies.find((d: any) => d.name === 'bash').path === '/bin/bash', 'ACTUAL_BASH');
    for (const d of dependencies) {
      requireValue(isAbsolute(d.path) && digest(ownedRead(realpathSync(d.path))) === d.sha256, 'HOOK_DEPENDENCY_CHANGED');
      const first = hookPath.split(':').map(dir => resolve(dir, d.name)).find(p => {
        try { accessSync(p, constants.X_OK); return lstatSync(realpathSync(p)).isFile(); } catch { return false; }
      });
      requireValue(first && realpathSync(first) === realpathSync(d.path), 'HOOK_PATH_SHADOW');
    }
    if (m.hook_map.some((h: any) => ['core/hooks/pre-bash-curl-block.sh', 'core/hooks/pre-edit-config-protection.sh'].includes(h.script)))
      requireValue(m.canonical_inputs.some((a: any) => a.path === 'core/bin/harness-python'), 'HELPER_UNSEALED');
  }
  function sealedClosure(m: RecordValue) {
    executionClosure(m);
    for (const a of m.canonical_inputs) requireValue(digest(ownedRead(resolve(m.source_root, a.path))) === a.sha256, 'SOURCE_CHANGED');
    for (const a of m.artifacts) requireValue(digest(ownedRead(resolve(bundleDir, a.relative_path))) === a.sha256, 'ARTIFACT_CHANGED');
  }
  const transforms: NonNullable<Services['transforms']> = {};
  registerRequired(api, {
    load() {
      const manifestBytes = ownedRead(resolve(bundleDir, 'selection.json'));
      loaded = JSON.parse(manifestBytes.toString());
      const { artifacts, effective_selection_sha256, ...sealed } = loaded;
      requireValue(digest(canonical(sealed)) === effective_selection_sha256 &&
        bundleDir.endsWith('/' + effective_selection_sha256), 'SELECTION_HASH');
      requireValue(artifacts.some((a: any) => a.relative_path === 'policy.md') &&
        artifacts.some((a: any) => a.relative_path === 'required-extension.ts'), 'REQUIRED_ARTIFACT');
      for (const a of loaded.artifacts) {
        const p = resolve(bundleDir, a.relative_path);
        requireValue(relative(bundleDir, p) === a.relative_path && digest(ownedRead(p)) === a.sha256 &&
          (lstatSync(p).mode & 0o777) === parseInt(a.mode, 8), 'ARTIFACT');
      }
      for (const a of loaded.canonical_inputs) requireValue(digest(ownedRead(resolve(loaded.source_root, a.path))) === a.sha256, 'SOURCE_CHANGED');
      // Several original guards deliberately return 0 when jq/python is absent.
      // Their prerequisite is therefore admission, never a guard-success fallback.
      const dependencies = loaded.hook_dependencies ?? [];
      hookPath = [...new Set(['python3', 'jq', 'bash'].flatMap(name =>
        dependencies.filter((d: any) => d.name === name).map((d: any) => dirname(d.path)))), '/usr/bin', '/bin'].join(':');
      executionClosure(loaded);
      hookPython = dependencies.find((d: any) => d.name === 'python3')?.path;
      for (const rewrite of loaded.bridge_rewrites) {
        transforms[rewrite.canonical_entry_id] = async (raw, signal) => {
          const path = resolve(loaded.source_root, rewrite.script);
          sealedClosure(loaded);
          requireValue(digest(ownedRead(path)) === rewrite.sha256, 'REWRITE_SOURCE');
          const result = await runScript('/bin/bash', [path], canonical(raw), signal, loaded.workspace, undefined, hookPath, hookPython);
          requireValue(result.code === 0, 'REWRITE_NONZERO');
          const parsed = JSON.parse(result.stdout);
          requireValue(object(parsed) && Object.keys(parsed).length === 1 && object(parsed.input), 'REWRITE_RESULT');
          return parsed.input;
        };
      }
      return { ...loaded, bundle_dir: bundleDir, manifest_sha256: digest(manifestBytes) };
    },
    async current(m, ctx) {
      sealedClosure(m); // Validate actual execution closure before invoking even the readonly selector.
      // Reuse the existing readonly projection selector. Launcher-owned public
      // lane supplies this exact descriptor; absence is a precise admission error.
      const p = m.current_projection;
      requireValue(object(p) && digest(ownedRead(p.helper)) === p.sha256, 'CURRENT_PROJECTION_MISSING');
      requireValue(typeof p.home === 'string' && isAbsolute(p.home) && realpathSync(p.home) === p.home &&
        p.registry === resolve(p.home, '.config/harness-launcher/profiles'), 'CURRENT_PROJECTION_HOME');
      requireValue(p.python === m.hook_dependencies?.find((d: any) => d.name === 'python3')?.path, 'CURRENT_INTERPRETER');
      requireValue(object(p.expected) && typeof p.expected.profile_binding_sha256 === 'string' &&
        typeof p.expected.binary_sha256 === 'string', 'CURRENT_EXPECTATION');
      const result = await runScript(p.python, [p.helper, 'select-continuation-source', p.source, p.registry, p.source, p.consumer],
        '', AbortSignal.timeout(2000), ctx.cwd, p.home);
      requireValue(result.code === 0, 'CURRENT_PROJECTION_REFUSED');
      const value = JSON.parse(result.stdout);
      requireValue(value.binary_release?.version === m.runtime_version &&
        value.binary_release?.source_revision === (m.schema === 2 ? m.native_release?.source_revision : '96894a6147d8cc64b7e00ad3a7d7178fd93b9531'), 'NATIVE_SOURCE_VERSION');
      if (m.schema === 2) {
        const { binary_sha256, ...release } = m.native_release;
        requireValue(canonical(value.binary_release) === canonical(release) && value.binary_sha256 === binary_sha256, 'NATIVE_RELEASE');
      }
      requireValue(Object.keys(p.expected).every(k => JSON.stringify(value[k]) === JSON.stringify(p.expected[k])), 'CURRENT_BINDING');
      requireValue(ctx.cwd === m.workspace, 'WORKSPACE');
      sealedClosure(m);
      return true;
    },
    async run(h, p, signal) {
      sealedClosure(loaded);
      const path = resolve(loaded.source_root, h.script);
      requireValue(digest(ownedRead(path)) === h.script_sha256, 'SCRIPT_CHANGED');
      return await runScript('/bin/bash', [path], JSON.stringify(p), signal, p.cwd, undefined, hookPath, hookPython);
    },
    transforms,
  });
}
