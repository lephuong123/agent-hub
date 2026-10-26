import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough, Writable } from 'node:stream';
import React from 'react';
import { Text, render } from 'ink';
import { highlightLine, selectedSpans, selectedText, Selection, stripAnsi } from './selection.js';
import { inkFrameReader } from './inkFrame.js';
import { osc52 } from './clipboard.js';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test('selectedSpans: stream selection in reading order, either drag direction', () => {
  const down = selectedSpans({ x: 4, y: 1 }, { x: 2, y: 3 }, 10);
  assert.deepEqual([...down], [
    [1, [4, 10]],
    [2, [0, 10]],
    [3, [0, 3]],
  ]);
  assert.deepEqual([...selectedSpans({ x: 2, y: 3 }, { x: 4, y: 1 }, 10)], [...down]);
  assert.deepEqual([...selectedSpans({ x: 5, y: 0 }, { x: 1, y: 0 }, 10)], [[0, [1, 6]]]);
});

test('selectedText strips styling, trailing blanks and the scrollbar column', () => {
  const frame = ['\x1b[32m● Bash\x1b[39m(ls)          │', '  ⎿  line 0              ┃', '     line 1              ┃', ''].join('\n');
  const spans = selectedSpans({ x: 2, y: 0 }, { x: 25, y: 2 }, 25);
  assert.equal(selectedText(frame, spans, 25), 'Bash(ls)\n  ⎿  line 0\n     line 1');
  // Wide characters count as two columns.
  assert.equal(selectedText('xin chào 世界!', selectedSpans({ x: 9, y: 0 }, { x: 12, y: 0 }, 20), 20), '世界');
});

test('highlightLine inverts the span and keeps styling around it', () => {
  const out = highlightLine('\x1b[31mred\x1b[39m plain', 1, 6);
  assert.equal(stripAnsi(out), 'red plain');
  assert.ok(out.startsWith('\x1b[31mr'), out);
  assert.ok(out.includes('\x1b[7med pl\x1b[0m'), JSON.stringify(out));
  assert.equal(stripAnsi(highlightLine('ab', 0, 4)), 'ab  ', 'short rows are padded so the span shows');
});

test('Selection paints the overlay, restores rows that leave it, and saves the cursor', () => {
  const writes: string[] = [];
  const frame = 'row0\nrow1\nrow2';
  const sel = new Selection(() => frame, (s) => writes.push(s), () => 10);
  sel.set({ x: 0, y: 0 }, { x: 1, y: 1 });
  assert.equal(writes.length, 1);
  assert.ok(writes[0]!.startsWith('\x1b7') && writes[0]!.endsWith('\x1b8'));
  assert.match(writes[0]!, /\x1b\[1;1H.*\x1b\[7mrow0/);
  assert.match(writes[0]!, /\x1b\[2;1H.*\x1b\[7mro\x1b\[0mw1/);
  assert.equal(sel.text(), 'row0\nro');
  sel.set({ x: 0, y: 1 }, { x: 3, y: 1 });
  assert.match(writes[1]!, /\x1b\[1;1H\x1b\[0m\x1b\[2Krow0/, 'row 0 put back as Ink drew it');
  sel.clear();
  assert.match(writes[2]!, /\x1b\[2;1H\x1b\[0m\x1b\[2Krow1/);
  sel.paint();
  assert.equal(writes.length, 3, 'nothing to paint once cleared');
  assert.equal(osc52('hi'), '\x1b]52;c;aGk=\x07');
});

test("inkFrameReader finds the frame Ink last drew (guards Ink's internal layout)", async () => {
  const stdout = Object.assign(new Writable({ write: (_c, _e, cb) => cb() }), { columns: 40, rows: 5, isTTY: true });
  const stdin = Object.assign(new PassThrough(), { isTTY: true, setRawMode() {}, ref() {}, unref() {} });
  const read = await inkFrameReader(stdout);
  assert.ok(read, 'Ink instances module not found: check src/ui/term/inkFrame.ts against the installed Ink');
  const app = render(<Text color="green">hello frame</Text>, { stdout: stdout as never, stdin: stdin as never, patchConsole: false });
  await sleep(50);
  assert.equal(stripAnsi(read()), 'hello frame');
  app.unmount();
});
