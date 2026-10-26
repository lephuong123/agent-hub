import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough, Writable } from 'node:stream';
import { createElement } from 'react';
import { render } from 'ink';
import { PromptInput, type PromptInputProps } from './PromptInput.js';

const strip = (s: string) => s.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '');
const tick = () => new Promise((r) => setTimeout(r, 30));

function mount(overrides: Partial<PromptInputProps> = {}) {
  const stdin = Object.assign(new PassThrough(), { isTTY: true, setRawMode() {}, ref() {}, unref() {} });
  let frame = '';
  const stdout = Object.assign(
    new Writable({
      write(chunk, _enc, cb) {
        frame = String(chunk);
        cb();
      },
    }),
    { columns: 80, rows: 24, isTTY: false },
  );
  const calls: { submit: string[]; exit: number; interrupt: number; cycle: number } = { submit: [], exit: 0, interrupt: 0, cycle: 0 };
  const props: PromptInputProps = {
    commands: [
      { name: 'help', description: 'Show help', source: 'local' },
      { name: 'clear', description: 'Clear transcript', source: 'local' },
      { name: 'compact', description: 'Compact context', source: 'remote' },
    ],
    busy: false,
    permissionMode: 'default',
    onSubmit: (t) => calls.submit.push(t),
    onExit: () => calls.exit++,
    onInterrupt: () => calls.interrupt++,
    onCyclePermissionMode: () => calls.cycle++,
    ...overrides,
  };
  const inst = render(createElement(PromptInput, props), {
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    debug: true,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  const press = async (...keys: string[]) => {
    for (const k of keys) {
      stdin.write(k);
      await tick();
    }
  };
  return { press, frame: () => strip(frame), calls, inst, rerender: (p: Partial<PromptInputProps>) => inst.rerender(createElement(PromptInput, { ...props, ...p })) };
}

test('PromptInput renders box, placeholder and footer', async () => {
  const ui = mount();
  await tick();
  const f = ui.frame();
  assert.match(f, /╭─+╮/);
  assert.match(f, /> Try "how does <filepath> work\?"/);
  assert.match(f, /\? for shortcuts/);
  ui.inst.unmount();
});

test('PromptInput typing, newline variants, submit', async () => {
  const ui = mount();
  await ui.press('h', 'i', '\\', '\r', 'x', '\n', 'y');
  assert.match(ui.frame(), /> hi\s*│[\s\S]*│\s+x\s*│[\s\S]*│\s+y/);
  await ui.press('\r');
  assert.deepEqual(ui.calls.submit, ['hi\nx\ny']);
  await ui.press('\r');
  assert.equal(ui.calls.submit.length, 1, 'empty submit ignored');
  await ui.press('\x1b[A');
  assert.match(ui.frame(), /> hi/, 'history recall');
  ui.inst.unmount();
});

test('PromptInput slash menu, Tab completion, Enter completes+submits', async () => {
  const ui = mount();
  await ui.press('/', 'c');
  let f = ui.frame();
  assert.match(f, /\/clear\s+Clear transcript/);
  assert.match(f, /\/compact\s+Compact context/);
  assert.doesNotMatch(f, /\/help/);
  await ui.press('\x1b[B', '\t');
  assert.match(ui.frame(), /> \/compact /);
  await ui.press('\x15', '/', 'h', 'e', '\r');
  assert.deepEqual(ui.calls.submit, ['/help']);
  ui.inst.unmount();
});

test('PromptInput Esc/Ctrl+C/Shift+Tab/? behaviour', async () => {
  const ui = mount();
  await ui.press('?');
  assert.match(ui.frame(), /shift\+tab to cycle modes/);
  await ui.press('?');
  assert.doesNotMatch(ui.frame(), /shift\+tab to cycle modes/);
  await ui.press('a', '\x1b');
  assert.match(ui.frame(), /Esc again to clear/);
  await ui.press('\x1b');
  assert.match(ui.frame(), /Try "how does/);
  await ui.press('\x03');
  assert.match(ui.frame(), /Press Ctrl\+C again to exit/);
  await ui.press('\x03');
  assert.equal(ui.calls.exit, 1);
  await ui.press('\x1b[Z');
  assert.equal(ui.calls.cycle, 1);
  ui.rerender({ busy: true, permissionMode: 'acceptEdits' });
  await ui.press('\x1b');
  assert.equal(ui.calls.interrupt, 1);
  assert.match(ui.frame(), /⏵⏵ accept edits on \(shift\+tab to cycle\)/);
  ui.inst.unmount();
});

test('PromptInput Backspace vs Delete, Vietnamese, bracketed large paste', async () => {
  const ui = mount();
  await ui.press('Xin chào', '\x7f', '\x1b[D', '\x1b[D', '\x1b[3~');
  assert.match(ui.frame(), /> Xin cà /, 'DEL=backspace, ESC[3~=forward delete');
  await ui.press('\x03');
  const big = Array.from({ length: 15 }, (_, i) => `row ${i}`).join('\r');
  await ui.press('see ', `\x1b[200~${big}\x1b[201~`);
  assert.match(ui.frame(), /> see \[Pasted text #1 \+15 lines\]/);
  await ui.press('\r');
  assert.equal(ui.calls.submit[0], `see ${big.replace(/\r/g, '\n')}`);
  ui.inst.unmount();
});

test('PromptInput ignores keys when disabled', async () => {
  const ui = mount({ disabled: true });
  await ui.press('a', '\r');
  assert.equal(ui.calls.submit.length, 0);
  assert.match(ui.frame(), /Try "how does/);
  ui.inst.unmount();
});
