import { PassThrough, Writable } from 'node:stream';
import { createElement } from 'react';
import { render } from 'ink';
import { HarnessSession } from '../src/core/session.js';
import { App } from '../src/App.js';

const strip = (s: string) => s.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const stdin = Object.assign(new PassThrough(), { isTTY: true, setRawMode() {}, ref() {}, unref() {} });
let frame = '';
const stdout = Object.assign(new Writable({ write(c, _e, cb) { frame = String(c); cb(); } }), { columns: 100, rows: 40, isTTY: false });

const session = new HarnessSession({ cwd: process.argv[2]! });
session.start();
const app = render(createElement(App, { session }), { stdin: stdin as any, stdout: stdout as any, debug: true, exitOnCtrlC: false, patchConsole: false });

const waitFor = async (pred: () => boolean, ms: number, label: string) => {
  const t0 = Date.now();
  while (!pred()) { if (Date.now() - t0 > ms) throw new Error('timeout: ' + label + '\n' + strip(frame)); await sleep(200); }
};
await sleep(1500);
for (const ch of 'Use the Bash tool to run: echo hello-cfh   then reply with the output only.') stdin.write(ch);
await sleep(200);
stdin.write('\r');
await waitFor(() => !!session.getState().pendingPermission, 90000, 'permission');
await sleep(300);
console.log('===== FRAME WITH PERMISSION DIALOG =====\n' + strip(frame));
stdin.write('1');
await sleep(500);
await waitFor(() => !session.getState().status.busy, 120000, 'result');
await sleep(800);
console.log('===== FINAL FRAME =====\n' + strip(frame));
await session.close();
app.unmount();
process.exit(0);
