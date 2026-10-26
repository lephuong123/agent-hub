import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as E from './editor.js';
import { matchCommands, slashQuery } from './slash.js';
import { expandPastes, isLargePaste, pasteToken } from './paste.js';
import type { SlashCommand } from '../../types.js';

const at = (value: string, cursor = value.length) => E.fromText(value, cursor);
const typeAll = (text: string, s = E.EMPTY) => [...text].reduce((st, ch) => E.insert(st, ch), s);

test('insert and backspace/delete', () => {
  let s = typeAll('hello');
  assert.deepEqual(s, { value: 'hello', cursor: 5 });
  s = E.backspace(s);
  assert.equal(s.value, 'hell');
  s = E.left(E.left(s));
  s = E.insert(s, 'XY');
  assert.deepEqual(s, { value: 'heXYll', cursor: 4 });
  s = E.del(s);
  assert.deepEqual(s, { value: 'heXYl', cursor: 4 });
  assert.equal(E.backspace(at('', 0)).value, '');
  assert.equal(E.del(at('ab')).value, 'ab');
});

test('pasted multi-line chunk normalises CRLF and strips control chars', () => {
  const s = E.insert(at('ab', 1), 'x\r\ny\rz\x07\tq');
  assert.equal(s.value, 'ax\ny\nz\tqb');
  assert.equal(s.cursor, 8);
});

test('applyChunk treats embedded DEL/BS as backspace (IME re-composition)', () => {
  let s = typeAll('Vie');
  s = E.applyChunk(s, '\x7fê');
  assert.equal(s.value, 'Viê');
  s = E.applyChunk(s, 't\b\x7fệt');
  assert.equal(s.value, 'Việt');
});

test('Vietnamese text: cursor moves by grapheme', () => {
  let s = typeAll('Xin chào tiếng Việt');
  assert.equal(s.value, 'Xin chào tiếng Việt');
  s = E.lineStart(s);
  for (let i = 0; i < 7; i++) s = E.right(s);
  assert.equal(E.graphemeAt(s), 'o');
  s = E.left(s);
  assert.equal(E.graphemeAt(s), 'à');
  s = E.del(s);
  assert.equal(s.value, 'Xin cho tiếng Việt');
});

test('combining marks are composed (NFC) and never split', () => {
  // "e" + combining circumflex + combining dot below, typed as separate code points
  let s = E.insert(E.EMPTY, 'Vie');
  s = E.insert(s, '̂');
  s = E.insert(s, '̣');
  s = E.insert(s, 't');
  assert.equal(s.value, 'Việt');
  assert.equal(s.value.length, 4);

  // decomposed text that NFC cannot fully compose stays one grapheme
  const raw = 'ą́b'; // á + ogonek
  let d = E.fromText(raw.normalize('NFC'), 0);
  d = E.right(d);
  assert.equal(E.graphemeAt(d), 'b');
  d = E.backspace(d);
  assert.equal(d.value, 'b');
});

test('emoji and ZWJ sequences are single graphemes', () => {
  let s = typeAll('a👨‍👩‍👧b');
  s = E.left(s);
  s = E.backspace(s);
  assert.equal(s.value, 'ab');
  s = E.insert(E.fromText('x', 0), '🇻🇳');
  assert.equal(s.cursor, 4);
  assert.equal(E.left(s).cursor, 0);
});

test('newline, home/end, multiline up/down keep goal column', () => {
  let s = typeAll('first line');
  s = E.newline(s);
  s = typeAll('ab', s);
  s = E.newline(s);
  s = typeAll('third line!', s);
  assert.deepEqual(E.position(s), { line: 2, col: 11 });
  s = E.up(s);
  assert.deepEqual(E.position(s), { line: 1, col: 2 });
  s = E.up(s);
  assert.deepEqual(E.position(s), { line: 0, col: 10 }, 'goal column restored on longer line');
  assert.equal(E.up(s), s, 'up on first line is a no-op (same object)');
  s = E.down(E.down(s));
  assert.deepEqual(E.position(s), { line: 2, col: 11 });
  assert.equal(E.down(s), s);
  assert.equal(E.lineStart(s).cursor, 'first line\nab\n'.length);
  assert.equal(E.lineEnd(E.fromText(s.value, 0)).cursor, 10);
  assert.equal(E.right(s).goalCol, undefined, 'horizontal moves reset goal column');
});

test('up/down count graphemes, not code units', () => {
  let s = E.fromText('tiếng Việt\nab cd efgh');
  s = E.up(E.left(s));
  assert.deepEqual(E.position(s), { line: 0, col: 9 });
  assert.equal(E.graphemeAt(s), 't');
  s = E.down(E.fromText('Việt\nabcdef', 4));
  assert.deepEqual(E.position(s), { line: 1, col: 4 });
});

test('first/last line detection', () => {
  const v = 'a\nb\nc';
  assert.ok(E.isOnFirstLine(at(v, 1)));
  assert.ok(!E.isOnFirstLine(at(v, 2)));
  assert.ok(E.isOnLastLine(at(v, 4)));
  assert.ok(!E.isOnLastLine(at(v, 3)));
  assert.ok(E.isOnFirstLine(at('\nx', 0)));
});

test('word movement and deletion', () => {
  const s = at('hello, thế giới  foo_bar');
  assert.equal(E.wordLeft(s).cursor, 'hello, thế giới  '.length);
  assert.equal(E.wordLeft(E.wordLeft(s)).cursor, 'hello, thế '.length);
  assert.equal(E.wordRight(at(s.value, 0)).cursor, 5);
  assert.equal(E.wordRight(at(s.value, 5)).cursor, 'hello, thế'.length);
  assert.equal(E.deleteWordBack(s).value, 'hello, thế giới  ');
  assert.equal(E.deleteWordBack(at('foo bar  ')).value, 'foo ');
  assert.equal(E.deleteWordForward(at('foo bar', 3)).value, 'foo');
  assert.equal(E.wordLeft(at('ab\ncd', 3)).cursor, 0, 'crosses lines');
});

test('Ctrl+U / Ctrl+K', () => {
  assert.deepEqual(E.deleteToLineStart(at('ab\ncdef', 5)), { value: 'ab\nef', cursor: 3 });
  assert.deepEqual(E.deleteToLineStart(at('ab\ncd', 3)), { value: 'abcd', cursor: 2 }, 'joins at line start');
  assert.deepEqual(E.deleteToLineStart(at('ab', 0)), { value: 'ab', cursor: 0 });
  assert.deepEqual(E.deleteToLineEnd(at('ab\ncdef', 4)), { value: 'ab\nc', cursor: 4 });
  assert.deepEqual(E.deleteToLineEnd(at('ab\ncd', 2)), { value: 'abcd', cursor: 2 }, 'joins at line end');
});

test('history: navigation, draft preservation, dedupe', () => {
  let h = E.createHistory(['one', 'two\nlines']);
  assert.equal(E.historyNext(h), null);
  const a = E.historyPrev(h, 'my draft')!;
  assert.equal(a.value, 'two\nlines');
  const ed = E.recalled(a.value, 'prev');
  assert.ok(E.isOnFirstLine(ed), 'recalled entry puts cursor on first line');
  const b = E.historyPrev(a.history, a.value)!;
  assert.equal(b.value, 'one');
  assert.equal(E.historyPrev(b.history, b.value), null);
  const c = E.historyNext(b.history)!;
  assert.equal(c.value, 'two\nlines');
  assert.ok(E.isOnLastLine(E.recalled(c.value, 'next')));
  const d = E.historyNext(c.history)!;
  assert.equal(d.value, 'my draft');
  assert.equal(E.historyNext(d.history), null);

  h = E.historyPush(d.history, 'three');
  h = E.historyPush(h, 'three');
  assert.deepEqual(h.entries, ['one', 'two\nlines', 'three']);
  assert.equal(h.index, 3);
  assert.equal(E.historyPrev(E.createHistory(), 'x'), null);
});

test('slash matching: exact, prefix, contains, fuzzy', () => {
  const cmds: SlashCommand[] = ['help', 'clear', 'compact', 'cost', 'model', 'config', 'mcp'].map((name) => ({
    name,
    description: '',
    source: 'local',
  }));
  const names = (q: string) => matchCommands(cmds, q).map((c) => c.name);
  assert.deepEqual(names('co'), ['compact', 'config', 'cost']);
  assert.deepEqual(names('mcp'), ['mcp']);
  assert.deepEqual(names('cpt'), ['compact']);
  assert.deepEqual(names('ost'), ['cost']);
  assert.deepEqual(names('cmt'), ['compact']);
  assert.equal(names('').length, cmds.length);
  assert.equal(slashQuery('/co'), 'co');
  assert.equal(slashQuery('/co x'), null);
  assert.equal(slashQuery('hi'), null);
});

test('large paste placeholder round-trips', () => {
  const big = Array.from({ length: 12 }, (_, i) => `line ${i}`).join('\n');
  assert.ok(isLargePaste(big));
  assert.ok(isLargePaste('x'.repeat(801)));
  assert.ok(!isLargePaste('short\ntext'));
  const token = pasteToken(1, big);
  assert.equal(token, '[Pasted text #1 +12 lines]');
  const value = `explain ${token} please`;
  assert.equal(expandPastes(value, new Map([[token, big]])), `explain ${big} please`);
});
