import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Options, PermissionResult, query, SDKMessage, SDKUserMessage } from '@anthropic-ai/claude-agent-sdk';
import { HarnessSession } from './session.js';
import { runLocalCommand } from './localCommands.js';
import type { SessionState } from '../types.js';

type Script = (prompt: AsyncIterable<SDKUserMessage>, options: Options) => AsyncGenerator<SDKMessage>;

function fakeQuery(script: Script, calls: string[] = []): typeof query {
  return (({ prompt, options }: { prompt: AsyncIterable<SDKUserMessage>; options: Options }) => {
    const gen = script(prompt, options);
    return Object.assign(gen, {
      interrupt: async () => void calls.push('interrupt'),
      setPermissionMode: async (m: string) => void calls.push(`mode:${m}`),
      setModel: async (m?: string) => void calls.push(`model:${m}`),
    });
  }) as unknown as typeof query;
}

const msg = (m: object) => m as unknown as SDKMessage;
const ev = (event: object, parent: string | null = null) => msg({ type: 'stream_event', event, parent_tool_use_id: parent, uuid: 'u', session_id: 's1' });

function waitFor(session: HarnessSession, pred: (s: SessionState) => boolean, ms = 2000): Promise<SessionState> {
  return new Promise((resolve, reject) => {
    if (pred(session.getState())) return resolve(session.getState());
    const timer = setTimeout(() => (unsub(), reject(new Error('timeout'))), ms);
    const unsub = session.subscribe(() => {
      if (pred(session.getState())) {
        clearTimeout(timer);
        unsub();
        resolve(session.getState());
      }
    });
  });
}

test('maps a full turn: init, text stream, tool use, tool result, result', async () => {
  let seenOptions: Options | undefined;
  const script: Script = async function* (prompt, options) {
    seenOptions = options;
    yield msg({
      type: 'system',
      subtype: 'init',
      model: 'claude-test',
      session_id: 's1',
      permissionMode: 'acceptEdits',
      slash_commands: ['review', 'clear', 'my-skill'],
    });
    for await (const user of prompt) {
      assert.equal(user.message.content, 'hello');
      yield ev({ type: 'message_start', message: { id: 'm1', usage: { output_tokens: 1 } } });
      yield ev({ type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } });
      yield ev({ type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: 'hmm' } });
      yield ev({ type: 'content_block_start', index: 1, content_block: { type: 'text', text: '' } });
      yield ev({ type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: 'Hel' } });
      yield ev({ type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: 'lo' } });
      yield ev({ type: 'content_block_start', index: 2, content_block: { type: 'tool_use', id: 't1', name: 'Bash', input: {} } });
      yield ev({ type: 'message_delta', delta: {}, usage: { output_tokens: 42 } });
      // Complete messages arrive one block at a time; text differs slightly (drift fix).
      const base = { type: 'assistant', parent_tool_use_id: null, session_id: 's1' };
      yield msg({ ...base, uuid: 'a1', message: { id: 'm1', content: [{ type: 'thinking', thinking: 'hmm.' }] } });
      yield msg({ ...base, uuid: 'a2', message: { id: 'm1', content: [{ type: 'text', text: 'Hello!' }] } });
      yield msg({ ...base, uuid: 'a3', message: { id: 'm1', content: [{ type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'ls' } }] } });
      yield msg({
        type: 'user',
        parent_tool_use_id: null,
        message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: [{ type: 'text', text: 'a.txt' }, { type: 'image' }] }] },
      });
      yield msg({
        type: 'result',
        subtype: 'success',
        is_error: false,
        result: 'Hello!',
        session_id: 's1',
        total_cost_usd: 0.0123,
        usage: { input_tokens: 10, output_tokens: 42, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
        modelUsage: { 'claude-test': { inputTokens: 10, outputTokens: 42, cacheReadInputTokens: 5, cacheCreationInputTokens: 0, costUSD: 0.0123 } },
      });
    }
  };
  const session = new HarnessSession({ cwd: process.cwd() }, { query: fakeQuery(script) });
  session.start();
  await waitFor(session, (s) => s.status.model === 'claude-test');
  const s0 = session.getState();
  assert.equal(s0.status.permissionMode, 'acceptEdits');
  assert.deepEqual(
    s0.slashCommands.filter((c) => c.source === 'remote').map((c) => c.name),
    ['review', 'my-skill'],
  );
  assert.equal(s0.slashCommands.filter((c) => c.name === 'clear').length, 1);
  assert.ok(seenOptions?.includePartialMessages);
  assert.deepEqual(seenOptions?.settingSources, ['user', 'project', 'local']);

  const snapshots: SessionState[] = [];
  session.subscribe(() => snapshots.push(session.getState()));
  session.send('hello');
  assert.equal(session.getState().status.busy, true);
  const s = await waitFor(session, (st) => !st.status.busy);

  const streaming = snapshots.find((st) => st.entries.some((e) => e.kind === 'assistant' && e.streaming && e.text === 'Hello'));
  assert.ok(streaming, 'saw streaming text');
  assert.ok(snapshots.some((st) => st.status.activity === 'Running Bash'));
  assert.ok(snapshots.some((st) => st.status.turnOutputTokens === 42));
  // immutability: earlier snapshot was not mutated
  assert.notEqual(streaming!.entries, s.entries);

  assert.deepEqual(
    s.entries.map((e) => e.kind),
    ['user', 'thinking', 'assistant', 'tool'],
  );
  const [, thinking, text, tool] = s.entries;
  assert.deepEqual(thinking, { kind: 'thinking', id: 'm1:0', text: 'hmm.', streaming: false });
  assert.equal(text!.kind === 'assistant' && text.text, 'Hello!');
  assert.equal(text!.kind === 'assistant' && text.streaming, false);
  assert.equal(tool!.kind, 'tool');
  if (tool!.kind === 'tool') {
    assert.deepEqual(tool.input, { command: 'ls' });
    assert.equal(tool.status, 'done');
    assert.equal(tool.result, 'a.txt\n[image]');
  }
  assert.equal(s.status.totalCostUsd, 0.0123);
  assert.equal(s.status.inputTokens, 15);
  assert.equal(s.status.outputTokens, 42);
  assert.equal(s.status.activity, undefined);
  await session.close();
});

test('permission round-trip: deny then allow_always, FIFO', async () => {
  let release!: () => void;
  const done = new Promise<void>((r) => (release = r));
  const results: (PermissionResult | null)[] = [];
  const suggestions = [{ type: 'addRules', rules: [{ toolName: 'Bash' }], behavior: 'allow', destination: 'session' }];
  const script: Script = async function* (prompt, options) {
    yield msg({ type: 'system', subtype: 'init', model: 'm', session_id: 's', permissionMode: 'default', slash_commands: [] });
    for await (const _ of prompt) {
      const signal = new AbortController().signal;
      const p1 = options.canUseTool!('Write', { file_path: 'a' }, { signal, toolUseID: 't1', suggestions: [] } as never);
      const p2 = options.canUseTool!('Bash', { command: 'ls' }, { signal, toolUseID: 't2', suggestions } as never);
      results.push(await p1, await p2);
      release();
      yield msg({ type: 'result', subtype: 'success', is_error: false, result: '', session_id: 's', total_cost_usd: 0, usage: {}, modelUsage: {} });
    }
  };
  const session = new HarnessSession({ cwd: '/' }, { query: fakeQuery(script) });
  session.start();
  session.send('go');
  const s1 = await waitFor(session, (s) => s.pendingPermission?.id === 't1');
  assert.equal(s1.pendingPermission!.toolName, 'Write');
  assert.equal(s1.pendingPermission!.canAllowAlways, false);
  session.answerPermission('t1', 'deny');
  const s2 = await waitFor(session, (s) => s.pendingPermission?.id === 't2');
  assert.equal(s2.pendingPermission!.canAllowAlways, true);
  session.answerPermission('t2', 'allow_always');
  await done;
  assert.deepEqual(results[0], { behavior: 'deny', message: 'User denied this action' });
  assert.deepEqual(results[1], { behavior: 'allow', updatedInput: { command: 'ls' }, updatedPermissions: suggestions });
  assert.equal(session.getState().pendingPermission, null);
  await session.close();
});

test('aborted permission request is dropped', async () => {
  const ac = new AbortController();
  let result: PermissionResult | null | undefined;
  const script: Script = async function* (prompt, options) {
    for await (const _ of prompt) {
      const p = options.canUseTool!('Bash', {}, { signal: ac.signal, toolUseID: 't9' } as never);
      yield msg({ type: 'system', subtype: 'status', status: null });
      result = await p;
    }
  };
  const session = new HarnessSession({ cwd: '/' }, { query: fakeQuery(script) });
  session.send('x');
  await waitFor(session, (s) => s.pendingPermission?.id === 't9');
  ac.abort();
  await waitFor(session, (s) => s.pendingPermission === null);
  await new Promise((r) => setImmediate(r));
  assert.equal(result?.behavior, 'deny');
  await session.close();
});

test('generator error becomes a system error entry', async () => {
  const script: Script = async function* () {
    throw new Error('Not logged in');
  };
  const session = new HarnessSession({ cwd: '/' }, { query: fakeQuery(script) });
  session.send('hi');
  const s = await waitFor(session, (st) => st.entries.some((e) => e.kind === 'system' && e.level === 'error'));
  assert.equal(s.status.busy, false);
  const err = s.entries.find((e) => e.kind === 'system');
  assert.match(err?.kind === 'system' ? err.text : '', /Not logged in/);
});

test('interrupt, local commands', async () => {
  const calls: string[] = [];
  const script: Script = async function* (prompt) {
    for await (const _ of prompt) yield ev({ type: 'message_start', message: { id: 'm1' } });
  };
  const session = new HarnessSession({ cwd: '/' }, { query: fakeQuery(script, calls) });
  session.send('long task');
  await session.interrupt();
  const s = session.getState();
  assert.equal(s.status.busy, false);
  assert.equal(s.entries.at(-1)!.kind === 'system' && (s.entries.at(-1) as { text: string }).text, 'Interrupted by user');

  let exited = false;
  const ctx = { exit: () => (exited = true) };
  assert.equal(runLocalCommand(session, 'mode', '', ctx), true);
  assert.equal(runLocalCommand(session, 'model', 'opus', ctx), true);
  await new Promise((r) => setImmediate(r));
  assert.equal(session.getState().status.permissionMode, 'acceptEdits');
  assert.equal(session.getState().status.model, 'opus');
  assert.deepEqual(calls, ['interrupt', 'mode:acceptEdits', 'model:opus']);
  runLocalCommand(session, 'help', '', ctx);
  assert.match((session.getState().entries.at(-1) as { text: string }).text, /\/mode \[default/);
  runLocalCommand(session, 'clear', '', ctx);
  assert.equal(session.getState().entries.length, 0);
  assert.equal(runLocalCommand(session, 'exit', '', ctx), true);
  assert.equal(exited, true);
  assert.equal(runLocalCommand(session, 'review', '', ctx), false);
  await session.close();
});
