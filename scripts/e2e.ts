// Live end-to-end run against the real Claude Code CLI (uses your login; costs a few cents).
// Usage: npx tsx scripts/e2e.ts <cwd> [--inline]
import { PassThrough, Writable } from 'node:stream';
import { createElement } from 'react';
import { render } from 'ink';
import { HarnessSession } from '../src/core/session.js';
import { App } from '../src/App.js';
import { InlineApp } from '../src/InlineApp.js';
import { MouseStdin } from '../src/ui/term/mouse.js';

const strip = (s: string) => s.replace(/\x1b\[[0-9;?<>=]*[A-Za-z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const inline = process.argv.includes('--inline');

const real = Object.assign(new PassThrough(), { isTTY: true, setRawMode() {}, ref() {}, unref() {} });
const mouse = new MouseStdin(real as never);
let frame = '';
const stdout = Object.assign(
  new Writable({
    write(c, _e, cb) {
      frame = String(c);
      cb();
    },
  }),
  { columns: 110, rows: 45, isTTY: true },
);
const screen = () => strip(frame);
const show = (label: string) => console.log(`===== ${label} =====\n${screen()}\n`);
const click = async (text: string) => {
  const lines = screen().split('\n');
  const row = lines.findIndex((l) => l.includes(text));
  if (row < 0) throw new Error(`not on screen: ${text}\n${screen()}`);
  const col = lines[row]!.indexOf(text) + 2;
  real.write(`\x1b[<0;${col};${row + 1}M\x1b[<0;${col};${row + 1}m`);
  await sleep(300);
};

const session = new HarnessSession({ cwd: process.argv[2]!, model: 'haiku', permissionMode: 'bypassPermissions', allowBypass: true });
session.start();
const app = render(createElement(inline ? InlineApp : App, { session, allowBypass: true, mouse }), {
  stdin: mouse as never,
  stdout: stdout as never,
  debug: true,
  exitOnCtrlC: false,
  patchConsole: false,
});

const waitFor = async (pred: () => boolean, ms: number, label: string) => {
  const t0 = Date.now();
  while (!pred()) {
    if (Date.now() - t0 > ms) throw new Error(`timeout: ${label}\n${screen()}`);
    await sleep(200);
  }
};

await sleep(1500);
real.write(
  'Use the Agent tool exactly once with subagent_type "general-purpose". Tell the subagent: run `echo hello-cfh` with Bash, then `sleep 8` with Bash, then reply with the echo output only. When it returns, reply with its answer only.',
);
await sleep(200);
real.write('\r');

await waitFor(() => session.getState().tasks.length > 0, 120_000, 'task_started');
await waitFor(() => session.getState().entries.some((e) => e.kind === 'tool' && e.parentToolUseId != null), 120_000, 'subagent tool call');
await sleep(800);
show('AGENT RUNNING');
if (!inline) {
  await click('Agent [general-purpose]');
  show('AGENT VIEW (clicked)');
  real.write('\x1b');
  await sleep(400);
}
await waitFor(() => !session.getState().status.busy, 180_000, 'turn end');
await sleep(800);
show('FINAL');
console.log('TASKS', JSON.stringify(session.getState().tasks, null, 2));
await session.close();
app.unmount();
process.exit(0);
