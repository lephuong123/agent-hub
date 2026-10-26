import {
  query as sdkQuery,
  type CanUseTool,
  type PermissionResult,
  type PermissionUpdate,
  type Query,
  type SDKMessage,
  type SDKUserMessage,
} from '@anthropic-ai/claude-agent-sdk';
import { existsSync } from 'node:fs';
import type {
  Entry,
  PermissionDecision,
  PermissionMode,
  PermissionRequest,
  Session,
  SessionOptions,
  SessionState,
  SessionStatus,
  SlashCommand,
} from '../types.js';
import { LOCAL_COMMANDS, PERMISSION_MODES } from './localCommands.js';

type ToolEntry = Extract<Entry, { kind: 'tool' }>;
type StreamedEntry = Extract<Entry, { kind: 'assistant' | 'thinking' }>;

export interface SessionDeps {
  query?: typeof sdkQuery;
}

/** Push-based async iterable used as the streaming-input prompt. */
class PushQueue<T> implements AsyncIterable<T> {
  private items: T[] = [];
  private waiters: ((r: IteratorResult<T>) => void)[] = [];
  private done = false;

  push(item: T): void {
    if (this.done) return;
    const w = this.waiters.shift();
    if (w) w({ value: item, done: false });
    else this.items.push(item);
  }

  end(): void {
    this.done = true;
    for (const w of this.waiters.splice(0)) w({ value: undefined, done: true });
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return {
      next: () => {
        if (this.items.length) return Promise.resolve({ value: this.items.shift()!, done: false });
        if (this.done) return Promise.resolve({ value: undefined, done: true });
        return new Promise((resolve) => this.waiters.push(resolve));
      },
    };
  }
}

interface PendingPermission {
  request: PermissionRequest;
  suggestions?: PermissionUpdate[];
  input: Record<string, unknown>;
  resolve: (r: PermissionResult) => void;
}

/** Per API message id: entries created from stream events, consumed in order by the complete message(s). */
interface StreamedBlocks {
  byIndex: Map<number, string>;
  text: string[];
  thinking: string[];
  usedText: number;
  usedThinking: number;
}

const TOOL_USE_TYPES = new Set(['tool_use', 'mcp_tool_use']);

export function toolResultText(content: unknown): string {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return JSON.stringify(content);
  return content
    .map((b: { type?: string; text?: string }) => {
      if (b?.type === 'text') return b.text ?? '';
      if (b?.type === 'image') return '[image]';
      if (b?.type === 'document') return '[document]';
      return `[${b?.type ?? 'unknown'}]`;
    })
    .join('\n');
}

const errMessage = (e: unknown) => (e instanceof Error ? e.message : String(e));

export class HarnessSession implements Session {
  private state: SessionState;
  private listeners = new Set<() => void>();
  private queue = new PushQueue<SDKUserMessage>();
  private q: Query | null = null;
  private readonly queryFn: typeof sdkQuery;
  private permissions: PendingPermission[] = [];
  private streams = new Map<string, StreamedBlocks>();
  private currentStream = new Map<string, string>(); // parent key -> API message id
  private turnTokensBase = 0;
  private interrupted = false;
  private closed = false;
  private seq = 0;
  private stderrTail: string[] = [];

  constructor(
    private readonly opts: SessionOptions,
    deps: SessionDeps = {},
  ) {
    this.queryFn = deps.query ?? sdkQuery;
    this.state = {
      entries: [],
      status: {
        busy: false,
        cwd: opts.cwd,
        model: opts.model,
        sessionId: opts.resume,
        permissionMode: opts.permissionMode ?? 'default',
        totalCostUsd: 0,
        inputTokens: 0,
        outputTokens: 0,
        turnOutputTokens: 0,
      },
      pendingPermission: null,
      slashCommands: [...LOCAL_COMMANDS],
    };
  }

  // ---- store ----

  getState = (): SessionState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(patch: Partial<Omit<SessionState, 'status'>> & { status?: Partial<SessionStatus> }): void {
    const { status, ...rest } = patch;
    this.state = { ...this.state, ...rest, status: status ? { ...this.state.status, ...status } : this.state.status };
    for (const l of [...this.listeners]) l();
  }

  private nextId(prefix: string): string {
    return `${prefix}-${++this.seq}`;
  }

  private addEntry(entry: Entry, status?: Partial<SessionStatus>): void {
    this.set({ entries: [...this.state.entries, entry], status });
  }

  private updateEntry<K extends Entry['kind']>(
    id: string,
    kind: K,
    fn: (e: Extract<Entry, { kind: K }>) => Extract<Entry, { kind: K }>,
    status?: Partial<SessionStatus>,
  ): boolean {
    const idx = this.state.entries.findIndex((e) => e.id === id && e.kind === kind);
    if (idx < 0) return false;
    const entries = this.state.entries.slice();
    entries[idx] = fn(entries[idx] as Extract<Entry, { kind: K }>);
    this.set({ entries, status });
    return true;
  }

  private finalizeStreaming(): Entry[] {
    return this.state.entries.map((e) =>
      (e.kind === 'assistant' || e.kind === 'thinking') && e.streaming ? { ...e, streaming: false } : e,
    );
  }

  // ---- lifecycle ----

  start(): void {
    if (this.q || this.closed) return;
    if (!existsSync(this.opts.cwd)) return this.fail(new Error(`Working directory does not exist: ${this.opts.cwd}`));
    const canUseTool: CanUseTool = (toolName, input, options) => this.requestPermission(toolName, input, options);
    try {
      this.q = this.queryFn({
        prompt: this.queue,
        options: {
          cwd: this.opts.cwd,
          model: this.opts.model,
          resume: this.opts.resume,
          permissionMode: this.state.status.permissionMode,
          allowDangerouslySkipPermissions: !!this.opts.allowBypass,
          includePartialMessages: true,
          canUseTool,
          settingSources: ['user', 'project', 'local'],
          systemPrompt: { type: 'preset', preset: 'claude_code' },
          stderr: (data) => {
            this.stderrTail.push(...data.split(/\r?\n/).filter(Boolean));
            this.stderrTail = this.stderrTail.slice(-20);
          },
        },
      });
    } catch (e) {
      this.fail(e);
      return;
    }
    void this.loop(this.q);
  }

  private async loop(q: Query): Promise<void> {
    try {
      for await (const msg of q) {
        try {
          this.handle(msg);
        } catch (e) {
          this.notify(`Internal error handling ${msg.type} message: ${errMessage(e)}`, 'error');
        }
      }
      if (!this.closed) this.notify('Claude Code session ended.', 'warn');
    } catch (e) {
      if (!this.closed) this.fail(e);
    } finally {
      this.q = null;
      this.dropPermissions();
      if (!this.closed)
        this.set({ entries: this.finalizeStreaming(), status: { busy: false, activity: undefined, turnStartedAt: undefined } });
    }
  }

  private fail(e: unknown): void {
    const tail = this.stderrTail.length ? `\n${this.stderrTail.join('\n')}` : '';
    this.addEntry(
      { kind: 'system', id: this.nextId('err'), text: `Error: ${errMessage(e)}${tail}`, level: 'error' },
      { busy: false, activity: undefined, turnStartedAt: undefined },
    );
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.queue.end();
    this.dropPermissions();
    const q = this.q;
    this.q = null;
    if (!q) return;
    try {
      if (typeof q.close === 'function') q.close();
      else await q.return(undefined);
    } catch {
      /* already gone */
    }
  }

  // ---- user actions ----

  send(text: string): void {
    if (this.closed) return;
    this.start();
    this.interrupted = false;
    if (!this.state.status.busy) this.turnTokensBase = 0;
    this.addEntry(
      { kind: 'user', id: this.nextId('user'), text },
      {
        busy: true,
        activity: 'Thinking',
        turnStartedAt: this.state.status.busy ? this.state.status.turnStartedAt : Date.now(),
        turnOutputTokens: this.state.status.busy ? this.state.status.turnOutputTokens : 0,
      },
    );
    this.queue.push({ type: 'user', message: { role: 'user', content: text }, parent_tool_use_id: null });
  }

  async interrupt(): Promise<void> {
    if (!this.state.status.busy) return;
    this.interrupted = true;
    this.dropPermissions();
    this.set({ entries: this.finalizeStreaming(), status: { busy: false, activity: undefined, turnStartedAt: undefined } });
    this.notify('Interrupted by user', 'warn');
    try {
      await this.q?.interrupt();
    } catch (e) {
      this.notify(`Interrupt failed: ${errMessage(e)}`, 'error');
    }
  }

  async setPermissionMode(mode: PermissionMode): Promise<void> {
    if (mode === 'bypassPermissions' && !this.opts.allowBypass)
      throw new Error('bypassPermissions requires starting cfharness with --dangerously-skip-permissions');
    if (this.q) await this.q.setPermissionMode(mode);
    this.set({ status: { permissionMode: mode } });
  }

  async setModel(model?: string): Promise<void> {
    if (this.q) await this.q.setModel(model);
    this.set({ status: { model } });
  }

  notify(text: string, level: 'info' | 'warn' | 'error' = 'info'): void {
    this.addEntry({ kind: 'system', id: this.nextId('sys'), text, level });
  }

  clearTranscript(): void {
    this.set({ entries: [] });
  }

  // ---- permissions ----

  private requestPermission(
    toolName: string,
    input: Record<string, unknown>,
    options: Parameters<CanUseTool>[2],
  ): Promise<PermissionResult> {
    const { signal, suggestions, toolUseID } = options;
    if (signal.aborted) return Promise.resolve({ behavior: 'deny', message: 'Aborted' });
    return new Promise<PermissionResult>((resolve) => {
      const pending: PendingPermission = {
        request: {
          id: toolUseID || this.nextId('perm'),
          toolName,
          input,
          reason: options.title ?? options.decisionReason ?? options.description,
          canAllowAlways: !!suggestions?.length && !options.suppressAlwaysAllowRule,
        },
        suggestions,
        input,
        resolve,
      };
      signal.addEventListener(
        'abort',
        () => {
          if (this.removePermission(pending)) resolve({ behavior: 'deny', message: 'Aborted' });
        },
        { once: true },
      );
      this.permissions.push(pending);
      this.syncPermission({ activity: 'Waiting for permission' });
    });
  }

  answerPermission(id: string, decision: PermissionDecision): void {
    const pending = this.permissions.find((p) => p.request.id === id);
    if (!pending) return;
    this.removePermission(pending);
    if (decision === 'deny') {
      pending.resolve({ behavior: 'deny', message: 'User denied this action' });
      this.updateEntry(id, 'tool', (e) => ({ ...e, status: 'denied' }));
      return;
    }
    pending.resolve({
      behavior: 'allow',
      updatedInput: pending.input,
      ...(decision === 'allow_always' && pending.suggestions ? { updatedPermissions: pending.suggestions } : {}),
    });
    this.set({ status: { activity: `Running ${pending.request.toolName}` } });
  }

  private removePermission(p: PendingPermission): boolean {
    const i = this.permissions.indexOf(p);
    if (i < 0) return false;
    this.permissions.splice(i, 1);
    this.syncPermission();
    return true;
  }

  private dropPermissions(): void {
    for (const p of this.permissions.splice(0)) p.resolve({ behavior: 'deny', message: 'Interrupted', interrupt: true });
    if (this.state.pendingPermission) this.syncPermission();
  }

  private syncPermission(status?: Partial<SessionStatus>): void {
    this.set({ pendingPermission: this.permissions[0]?.request ?? null, status });
  }

  // ---- message mapping ----

  private handle(msg: SDKMessage): void {
    switch (msg.type) {
      case 'system':
        return this.handleSystem(msg);
      case 'stream_event':
        return this.handleStream(msg);
      case 'assistant':
        return this.handleAssistant(msg);
      case 'user':
        return this.handleUser(msg);
      case 'result':
        return this.handleResult(msg);
    }
  }

  private toMode(mode: string | undefined): PermissionMode {
    return PERMISSION_MODES.includes(mode as PermissionMode) ? (mode as PermissionMode) : this.state.status.permissionMode;
  }

  private handleSystem(msg: Extract<SDKMessage, { type: 'system' }>): void {
    if (msg.subtype === 'init') {
      this.set({
        status: { model: msg.model, sessionId: msg.session_id, permissionMode: this.toMode(msg.permissionMode) },
        slashCommands: this.mergeCommands(msg.slash_commands.map((name) => ({ name, description: '', source: 'remote' }))),
      });
      void this.loadCommandDescriptions();
    } else if (msg.subtype === 'status') {
      if (msg.permissionMode) this.set({ status: { permissionMode: this.toMode(msg.permissionMode) } });
      if (msg.status === 'compacting') this.set({ status: { activity: 'Compacting' } });
    } else if (msg.subtype === 'compact_boundary') {
      this.notify('Conversation compacted');
    }
  }

  private mergeCommands(remote: SlashCommand[]): SlashCommand[] {
    const local = new Set(LOCAL_COMMANDS.map((c) => c.name));
    return [...LOCAL_COMMANDS, ...remote.filter((c) => !local.has(c.name))];
  }

  private async loadCommandDescriptions(): Promise<void> {
    const q = this.q;
    if (!q || typeof q.supportedCommands !== 'function') return;
    try {
      const info = new Map((await q.supportedCommands()).map((c) => [c.name, c]));
      const remote = this.state.slashCommands
        .filter((c) => c.source === 'remote')
        .map((c) => {
          const i = info.get(c.name);
          return i ? { ...c, description: i.description, argumentHint: i.argumentHint || undefined } : c;
        });
      this.set({ slashCommands: this.mergeCommands(remote) });
    } catch {
      /* descriptions are optional */
    }
  }

  private streamFor(msgId: string): StreamedBlocks {
    let s = this.streams.get(msgId);
    if (!s) this.streams.set(msgId, (s = { byIndex: new Map(), text: [], thinking: [], usedText: 0, usedThinking: 0 }));
    return s;
  }

  private handleStream(msg: Extract<SDKMessage, { type: 'stream_event' }>): void {
    const ev = msg.event;
    const parentKey = msg.parent_tool_use_id ?? '';
    if (ev.type === 'message_start') {
      this.currentStream.set(parentKey, ev.message.id);
      this.turnTokensBase = this.state.status.turnOutputTokens;
      return;
    }
    if (ev.type === 'message_delta') {
      this.set({ status: { turnOutputTokens: this.turnTokensBase + (ev.usage?.output_tokens ?? 0) } });
      return;
    }
    const msgId = this.currentStream.get(parentKey);
    if (!msgId) return;
    const s = this.streamFor(msgId);
    if (ev.type === 'content_block_start') {
      const block = ev.content_block;
      const id = `${msgId}:${ev.index}`;
      if (block.type === 'text') {
        s.byIndex.set(ev.index, id);
        s.text.push(id);
        this.addEntry(
          { kind: 'assistant', id, text: block.text ?? '', streaming: true, parentToolUseId: msg.parent_tool_use_id },
          { activity: 'Writing' },
        );
      } else if (block.type === 'thinking') {
        s.byIndex.set(ev.index, id);
        s.thinking.push(id);
        this.addEntry({ kind: 'thinking', id, text: block.thinking ?? '', streaming: true }, { activity: 'Thinking' });
      } else if (TOOL_USE_TYPES.has(block.type) && 'id' in block && 'name' in block) {
        this.upsertTool(block.id, block.name, {}, msg.parent_tool_use_id);
      }
    } else if (ev.type === 'content_block_delta') {
      const id = s.byIndex.get(ev.index);
      if (!id) return;
      if (ev.delta.type === 'text_delta') {
        const t = ev.delta.text;
        this.updateEntry(id, 'assistant', (e) => ({ ...e, text: e.text + t }), { activity: 'Writing' });
      } else if (ev.delta.type === 'thinking_delta') {
        const t = ev.delta.thinking;
        this.updateEntry(id, 'thinking', (e) => ({ ...e, text: e.text + t }), { activity: 'Thinking' });
      }
    }
  }

  private upsertTool(id: string, name: string, input: Record<string, unknown>, parentToolUseId: string | null): void {
    const status = { activity: `Running ${name}` };
    const exists = this.updateEntry(id, 'tool', (e) => ({ ...e, name, input, parentToolUseId }), status);
    if (!exists) this.addEntry({ kind: 'tool', id, name, input, status: 'running', parentToolUseId }, status);
  }

  private finishStreamed(
    s: StreamedBlocks | undefined,
    kind: 'text' | 'thinking',
    text: string,
    fallback: () => StreamedEntry,
  ): void {
    const ids = kind === 'text' ? s?.text : s?.thinking;
    const used = kind === 'text' ? s?.usedText ?? 0 : s?.usedThinking ?? 0;
    const id = ids?.[used];
    if (s && id) {
      if (kind === 'text') s.usedText++;
      else s.usedThinking++;
      const k = kind === 'text' ? 'assistant' : 'thinking';
      if (this.updateEntry(id, k, (e) => ({ ...e, text, streaming: false }))) return;
    }
    if (text) this.addEntry(fallback());
  }

  private handleAssistant(msg: Extract<SDKMessage, { type: 'assistant' }>): void {
    const m = msg.message;
    const s = this.streams.get(m.id);
    const parent = msg.parent_tool_use_id;
    m.content.forEach((block, i) => {
      const fresh = `${m.id}:${msg.uuid}:${i}`;
      if (block.type === 'text') {
        this.finishStreamed(s, 'text', block.text, () => ({
          kind: 'assistant',
          id: fresh,
          text: block.text,
          streaming: false,
          parentToolUseId: parent,
        }));
      } else if (block.type === 'thinking') {
        this.finishStreamed(s, 'thinking', block.thinking, () => ({
          kind: 'thinking',
          id: fresh,
          text: block.thinking,
          streaming: false,
        }));
      } else if (TOOL_USE_TYPES.has(block.type) && 'id' in block && 'name' in block) {
        const input = 'input' in block && block.input && typeof block.input === 'object' ? (block.input as Record<string, unknown>) : {};
        this.upsertTool(block.id, block.name, input, parent);
      }
    });
  }

  private handleUser(msg: Extract<SDKMessage, { type: 'user' }>): void {
    const content = msg.message.content;
    if (!Array.isArray(content)) return;
    for (const block of content) {
      if (block.type !== 'tool_result') continue;
      const result = toolResultText(block.content);
      const isError = !!block.is_error;
      this.updateEntry(
        block.tool_use_id,
        'tool',
        (e: ToolEntry) => ({ ...e, result, status: e.status === 'denied' ? 'denied' : isError ? 'error' : 'done' }),
        this.state.status.busy ? { activity: 'Thinking' } : undefined,
      );
    }
  }

  private handleResult(msg: Extract<SDKMessage, { type: 'result' }>): void {
    const models = Object.values(msg.modelUsage ?? {});
    const tokens = models.length
      ? {
          inputTokens: models.reduce((n, u) => n + u.inputTokens + u.cacheReadInputTokens + u.cacheCreationInputTokens, 0),
          outputTokens: models.reduce((n, u) => n + u.outputTokens, 0),
        }
      : {
          inputTokens:
            this.state.status.inputTokens +
            (msg.usage?.input_tokens ?? 0) +
            (msg.usage?.cache_read_input_tokens ?? 0) +
            (msg.usage?.cache_creation_input_tokens ?? 0),
          outputTokens: this.state.status.outputTokens + (msg.usage?.output_tokens ?? 0),
        };
    const more = (msg.queued_turn_count ?? 0) > 0;
    // Tool entries still running at turn end never got a result (e.g. interrupted).
    const entries = this.finalizeStreaming().map((e) =>
      e.kind === 'tool' && e.status === 'running' && !more ? { ...e, status: 'error' as const } : e,
    );
    this.set({
      entries,
      status: {
        ...tokens,
        totalCostUsd: msg.total_cost_usd ?? this.state.status.totalCostUsd,
        sessionId: msg.session_id || this.state.status.sessionId,
        busy: more,
        activity: more ? 'Thinking' : undefined,
        turnStartedAt: more ? this.state.status.turnStartedAt : undefined,
      },
    });
    this.streams.clear();
    this.currentStream.clear();
    if (this.interrupted) {
      this.interrupted = false;
      return;
    }
    if (msg.subtype !== 'success') {
      this.notify(msg.errors?.length ? msg.errors.join('\n') : `Turn ended: ${msg.subtype}`, 'error');
    } else if (msg.is_error) {
      this.notify(msg.result || 'Turn ended with an error', 'error');
    }
  }
}
