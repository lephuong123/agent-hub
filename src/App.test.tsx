import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough, Writable } from 'node:stream';
import React from 'react';
import { render } from 'ink';
import type { Entry, Session, SessionState, TaskInfo } from './types.js';
import { App } from './App.js';
import { InlineApp } from './InlineApp.js';
import { MouseStdin } from './ui/term/mouse.js';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const strip = (s: string) => s.replace(/\x1b\[[0-9;?<>=]*[A-Za-z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');

const now = Date.now();
const tool = (id: string, name: string, input: Record<string, unknown>, extra: Partial<Extract<Entry, { kind: 'tool' }>> = {}): Entry => ({
  kind: 'tool',
  id,
  name,
  input,
  status: 'done',
  parentToolUseId: null,
  startedAt: now - 5000,
  endedAt: now - 4000,
  ...extra,
});

const longOutput = Array.from({ length: 30 }, (_, i) => `line ${i}`).join('\n');

const entries: Entry[] = [
  { kind: 'user', id: 'u1', text: 'explore the repo' },
  tool('b1', 'Bash', { command: 'ls -R' }, { result: longOutput }),
  tool('ag', 'Agent', { description: 'Map the modules', prompt: 'List every module and what it does.' }, { status: 'running', endedAt: undefined }),
  { kind: 'thinking', id: 'th', text: 'start with src', streaming: false, parentToolUseId: 'ag' },
  tool('c1', 'Read', { file_path: 'src/session.ts' }, { parentToolUseId: 'ag', result: 'a\nb' }),
  tool('c2', 'Grep', { pattern: 'export' }, { parentToolUseId: 'ag', result: 'Found 4 files' }),
  { kind: 'assistant', id: 'a1', text: 'Working on it.', streaming: false, parentToolUseId: null },
];

const task: TaskInfo = {
  id: 'task1',
  toolUseId: 'ag',
  type: 'local_agent',
  description: 'Map the modules',
  subagentType: 'Explore',
  status: 'running',
  backgrounded: false,
  startedAt: now - 5000,
  tokens: 4200,
  toolUses: 2,
  lastToolName: 'Grep',
  summary: 'Reading session.ts',
};

function fakeSession(): Session & { calls: string[] } {
  const calls: string[] = [];
  const state: SessionState = {
    entries,
    status: {
      busy: true,
      activity: 'Running Agent',
      turnStartedAt: now - 5000,
      cwd: '/repo',
      model: 'claude-test',
      permissionMode: 'default',
      totalCostUsd: 0,
      inputTokens: 0,
      outputTokens: 0,
      turnOutputTokens: 0,
    },
    pendingPermission: null,
    slashCommands: [],
    tasks: [task],
  };
  return {
    calls,
    getState: () => state,
    subscribe: () => () => {},
    start() {},
    send: (t) => void calls.push(`send:${t}`),
    interrupt: async () => void calls.push('interrupt'),
    stopTask: async (id) => void calls.push(`stop:${id}`),
    backgroundTasks: async () => void calls.push('background'),
    answerPermission() {},
    setPermissionMode: async () => {},
    setModel: async () => {},
    notify: (t) => void calls.push(`notify:${t}`),
    clearTranscript() {},
    close: async () => {},
  };
}

test('fullscreen app: click to expand, open an agent, task panel keys, wheel scroll', async (t) => {
  const rows = 40;
  const real = Object.assign(new PassThrough(), { isTTY: true, setRawMode() {}, ref() {}, unref() {} });
  const stdin = new MouseStdin(real as never);
  let frame = '';
  const stdout = Object.assign(
    new Writable({
      write(c, _e, cb) {
        frame = String(c);
        cb();
      },
    }),
    { columns: 100, rows, isTTY: true },
  );
  const screen = () => strip(frame);
  const lines = () => screen().split('\n');
  const rowOf = (text: string) => lines().findIndex((l) => l.includes(text));
  const click = async (row: number, col = 5) => {
    real.write(`\x1b[<0;${col + 1};${row + 1}M\x1b[<0;${col + 1};${row + 1}m`);
    await sleep(120);
  };
  const key = async (seq: string) => {
    real.write(seq);
    await sleep(150);
  };

  const session = fakeSession();
  const app = render(<App session={session} mouse={stdin} />, {
    stdin: stdin as never,
    stdout: stdout as never,
    debug: true,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  t.after(() => app.unmount());
  await sleep(300);

  // Collapsed: Bash shows 5 lines and a click hint; agent shows its live summary and stats; the panel lists the task.
  assert.match(screen(), /… \+25 lines \(click to expand\)/);
  assert.match(screen(), /Agent \[Explore\]\(Map the modules\)/);
  assert.match(screen(), /Reading session\.ts/);
  assert.match(screen(), /2 tool uses · 4\.2k tokens · \d+s · click to open/);
  assert.match(screen(), /1 task running · ↓ or click to manage/);
  assert.match(screen(), /◐|◓|◑|◒/);
  assert.equal(lines().length, rows, 'the frame fills the terminal exactly');

  // Click the Bash tool: the whole output shows.
  await click(rowOf('Bash(ls -R)'));
  assert.match(screen(), /line 29/);
  assert.doesNotMatch(screen(), /\+25 lines/);

  // Click the agent: its detail view shows the prompt, its own entries and the stats.
  await click(rowOf('Agent [Explore]'));
  assert.match(screen(), /Prompt/);
  assert.match(screen(), /List every module and what it does\./);
  assert.match(screen(), /Read\(src\/session\.ts\)/);
  assert.match(screen(), /Grep\(pattern: "export"\)/);
  assert.match(screen(), /running · 2 tool uses · 4\.2k tokens/);
  assert.match(screen(), /\[esc back\]/);
  assert.doesNotMatch(screen(), /Try "how does/, 'the prompt is hidden in the task view');

  // x stops the task; Esc goes back.
  await key('x');
  assert.deepEqual(session.calls, ['stop:task1']);
  await key('\x1b');
  assert.match(screen(), /Try "how does|explore the repo/);
  assert.doesNotMatch(screen(), /\[esc back\]/);

  // ↓ from the prompt focuses the task panel; Enter opens the selected task.
  await key('\x1b[B');
  assert.match(screen(), /❯ ◐|❯ ◓|❯ ◑|❯ ◒/);
  assert.match(screen(), /enter or click to open/);
  await key('\r');
  assert.match(screen(), /\[esc back\]/);
  // Clicking [esc back] returns too, and focus goes back to the prompt with Esc.
  await click(rowOf('[esc back]'), lines()[rowOf('[esc back]')]!.indexOf('[esc back]') + 2);
  assert.doesNotMatch(screen(), /\[esc back\]/);
  await key('\x1b');
  assert.match(screen(), /↓ or click to manage/);

  // Clicking a task row opens it.
  await click(rowOf('Map the modules  '));
  assert.match(screen(), /\[esc back\]/);
  await key('\x1b');

  // Wheel up scrolls the transcript and shows the jump-to-bottom bar; PgDn returns to the bottom.
  await key('\x1b[<64;10;10M');
  await key('\x1b[<64;10;10M');
  assert.match(screen(), /more lines? · click, End or wheel/);
  await key('\x1b[6~');
  await key('\x1b[6~');
  assert.doesNotMatch(screen(), /more lines? · click/);

  // Typing still works and submits.
  await key('hi');
  await key('\r');
  assert.deepEqual(session.calls.slice(-1), ['send:hi']);
});

test('inline app still prints the transcript into scrollback on Ink 8', async (t) => {
  let out = '';
  const stdin = Object.assign(new PassThrough(), { isTTY: true, setRawMode() {}, ref() {}, unref() {} });
  const stdout = Object.assign(
    new Writable({
      write(c, _e, cb) {
        out += String(c);
        cb();
      },
    }),
    { columns: 100, rows: 40, isTTY: true },
  );
  const session = fakeSession();
  const app = render(<InlineApp session={session} />, { stdin: stdin as never, stdout: stdout as never, exitOnCtrlC: false, patchConsole: false });
  t.after(() => app.unmount());
  await sleep(300);
  const text = strip(out);
  assert.match(text, /Welcome to cfharness/);
  assert.match(text, /Bash\(ls -R\)/);
  assert.match(text, /… \+25 lines\n/, 'no click hint in the inline UI');
  assert.match(text, /Agent \[Explore\]\(Map the modules\)/);
  stdin.write('hi');
  await sleep(100);
  stdin.write('\r');
  await sleep(100);
  assert.deepEqual(session.calls.slice(-1), ['send:hi']);
});
