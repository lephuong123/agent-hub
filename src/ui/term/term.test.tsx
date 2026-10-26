import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough, Writable } from 'node:stream';
import React, { useState } from 'react';
import { Box, Text, render } from 'ink';
import { decodeMouse, extractMouse, MouseStdin } from './mouse.js';
import { Clickable, MouseProvider } from './MouseContext.js';
import { ScrollView, thumb } from './ScrollView.js';

test('extractMouse splits SGR reports from keystrokes and holds back partial sequences', () => {
  const r = extractMouse('ab\x1b[<0;5;3Mc\x1b[<0;5;3m\x1b[<65;1;1M\x1b[<3');
  assert.equal(r.text, 'abc');
  assert.equal(r.rest, '\x1b[<3');
  assert.deepEqual(
    r.events.map((e) => [e.action, e.button, e.x, e.y]),
    [
      ['press', 'left', 4, 2],
      ['release', 'left', 4, 2],
      ['wheelDown', 'none', 0, 0],
    ],
  );
  assert.equal(extractMouse('\x1b').rest, '\x1b', 'a lone ESC may start a report');
  assert.equal(extractMouse('\x1b[A').text, '\x1b[A', 'arrow keys pass through');
  assert.deepEqual(decodeMouse(32, 2, 2, false).action, 'drag');
  assert.deepEqual(decodeMouse(64 + 16, 1, 1, false), { action: 'wheelUp', button: 'none', x: 0, y: 0, shift: false, meta: false, ctrl: true });
});

test('thumb size and position', () => {
  assert.deepEqual(thumb(10, 5, 0), { start: 0, size: 10 });
  assert.deepEqual(thumb(10, 100, 0), { start: 0, size: 1 });
  assert.deepEqual(thumb(10, 100, 90), { start: 9, size: 1 });
  assert.deepEqual(thumb(10, 20, 5), { start: 3, size: 5 });
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const strip = (s: string) => s.replace(/\x1b\[[0-9;?<>=]*[A-Za-z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');

function harness(rows: number, cols: number) {
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
    { columns: cols, rows, isTTY: true },
  );
  const send = (s: string) => real.write(s);
  const lines = () => strip(frame).split('\n');
  return { stdin, stdout, send, lines };
}

test('ScrollView pins to the bottom, scrolls with the wheel, and routes clicks to the item under the pointer', async () => {
  const h = harness(8, 30);
  const clicked: number[] = [];
  let grow: (n: number) => void = () => {};
  function Demo() {
    const [n, setN] = useState(20);
    grow = setN;
    return (
      <MouseProvider source={h.stdin}>
        <Box flexDirection="column" height={8} width={30}>
          <ScrollView>
            {Array.from({ length: n }, (_, i) => (
              <Clickable key={i} onClick={() => clicked.push(i)}>
                <Text>{`item ${i}`}</Text>
              </Clickable>
            ))}
          </ScrollView>
          <Text>prompt</Text>
        </Box>
      </MouseProvider>
    );
  }
  const app = render(<Demo />, { stdin: h.stdin as never, stdout: h.stdout as never, debug: true, exitOnCtrlC: false, patchConsole: false });
  await sleep(100);
  // 7 rows of viewport above the prompt; pinned to the bottom.
  assert.match(h.lines()[0]!, /item 13/);
  assert.match(h.lines()[6]!, /item 19/);
  assert.match(h.lines()[7]!, /prompt/);

  // Click row 2 → item 15 (press + release).
  h.send('\x1b[<0;3;3M\x1b[<0;3;3m');
  await sleep(50);
  assert.deepEqual(clicked, [15]);

  // Wheel up 3 rows: unpinned, "more lines" bar takes one row.
  h.send('\x1b[<64;3;3M');
  await sleep(100);
  assert.match(h.lines()[0]!, /item 10/);
  assert.match(h.lines().join('\n'), /↓ \d+ more lines/);

  // Growing content does not move an unpinned view.
  grow(25);
  await sleep(100);
  assert.match(h.lines()[0]!, /item 10/);

  // Click on the "more lines" bar jumps to the bottom and re-pins.
  const barRow = h.lines().findIndex((l) => l.includes('more lines'));
  h.send(`\x1b[<0;3;${barRow + 1}M\x1b[<0;3;${barRow + 1}m`);
  await sleep(100);
  assert.match(h.lines()[6]!, /item 24/);
  grow(26);
  await sleep(100);
  assert.match(h.lines()[6]!, /item 25/);

  // Clicks on rows scrolled out of view never reach hidden items.
  h.send('\x1b[<0;3;1M\x1b[<0;3;1m');
  await sleep(50);
  assert.equal(clicked.at(-1), 19);
  app.unmount();
});
