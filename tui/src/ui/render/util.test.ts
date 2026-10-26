import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildDiff,
  cleanResult,
  closeOpenFences,
  countFound,
  diffLines,
  diffStats,
  formatCost,
  formatElapsed,
  formatTokens,
  parseTodos,
  toolDisplayName,
  toolSummary,
  truncateLines,
} from './util.js';
import { planTranscript } from './plan.js';
import { renderMarkdown } from './Markdown.js';
import type { Entry } from '../../types.js';

test('closeOpenFences closes an odd fence only', () => {
  assert.equal(closeOpenFences('a\n```ts\nconst x'), 'a\n```ts\nconst x\n```');
  assert.equal(closeOpenFences('```\nx\n```\n'), '```\nx\n```\n');
  assert.equal(closeOpenFences('````\n```\n'), '````\n```\n````');
  assert.equal(closeOpenFences('~~~\nx'), '~~~\nx\n~~~');
  assert.equal(closeOpenFences('no fences'), 'no fences');
});

test('truncateLines', () => {
  assert.deepEqual(truncateLines([1, 2, 3], 5), { shown: [1, 2, 3], hidden: 0 });
  assert.deepEqual(truncateLines([1, 2, 3, 4], 2), { shown: [1, 2], hidden: 2 });
});

test('diffLines keeps common lines as context', () => {
  const d = diffLines('a\nb\nc', 'a\nB\nc');
  assert.deepEqual(d, [
    { type: 'ctx', text: 'a' },
    { type: 'del', text: 'b' },
    { type: 'add', text: 'B' },
    { type: 'ctx', text: 'c' },
  ]);
  assert.deepEqual(diffStats(d), { added: 1, removed: 1 });
  assert.deepEqual(diffLines('', 'x'), [{ type: 'add', text: 'x' }]);
});

test('buildDiff for Edit / MultiEdit / Write / other', () => {
  assert.deepEqual(diffStats(buildDiff('Edit', { old_string: 'x', new_string: 'y\nz' })!), { added: 2, removed: 1 });
  const multi = buildDiff('MultiEdit', { edits: [{ old_string: 'a', new_string: 'b' }, { old_string: 'c', new_string: 'd' }] })!;
  assert.deepEqual(diffStats(multi), { added: 2, removed: 2 });
  assert.deepEqual(buildDiff('Write', { content: 'l1\nl2\n' }), [
    { type: 'add', text: 'l1' },
    { type: 'add', text: 'l2' },
  ]);
  assert.equal(buildDiff('Bash', { command: 'ls' }), null);
});

test('toolSummary and display name', () => {
  assert.equal(toolSummary('Bash', { command: 'npm test\nmore' }), 'npm test');
  assert.equal(toolSummary('Read', { file_path: '/a/b.ts' }), '/a/b.ts');
  assert.equal(toolSummary('Grep', { pattern: 'foo', path: 'src' }), 'pattern: "foo", path: "src"');
  assert.equal(toolSummary('TodoWrite', { todos: [] }), '');
  assert.equal(toolDisplayName('mcp__github__get_issue'), 'github - get_issue (MCP)');
});

test('parseTodos, countFound, cleanResult', () => {
  assert.deepEqual(parseTodos({ todos: [{ content: 'a', status: 'completed' }, { content: 'b', status: 'weird' }] }), [
    { content: 'a', status: 'completed', activeForm: undefined },
    { content: 'b', status: 'pending', activeForm: undefined },
  ]);
  assert.equal(countFound('Found 3 files\na\nb\nc'), 3);
  assert.equal(countFound('a\nb\n'), 2);
  assert.equal(countFound('No files found'), 0);
  assert.equal(cleanResult('x\n<system-reminder>hi</system-reminder>\n'), 'x');
});

test('formatters', () => {
  assert.equal(formatTokens(950), '950');
  assert.equal(formatTokens(1234), '1.2k');
  assert.equal(formatTokens(2000), '2k');
  assert.equal(formatCost(0.01234), '$0.0123');
  assert.equal(formatElapsed(12_400), '12s');
  assert.equal(formatElapsed(75_000), '1m 15s');
});

test('planTranscript groups subagent entries and finds finished prefix', () => {
  const entries: Entry[] = [
    { kind: 'user', id: 'u', text: 'hi' },
    { kind: 'tool', id: 't1', name: 'Task', startedAt: 0, input: {}, status: 'running', parentToolUseId: null },
    { kind: 'tool', id: 'c1', name: 'Read', startedAt: 0, input: {}, status: 'done', parentToolUseId: 't1' },
    { kind: 'assistant', id: 'a', text: 'x', streaming: false, parentToolUseId: null },
  ];
  const { items, finishedPrefix } = planTranscript(entries);
  assert.deepEqual(items.map((i) => i.entry.id), ['u', 't1', 'a']);
  assert.deepEqual(items[1]!.children!.map((c) => c.id), ['c1']);
  assert.equal(finishedPrefix, 1);
});

test('renderMarkdown handles unclosed fences and list inline markup', () => {
  const out = renderMarkdown('- item **bold**\n\n```js\nconst a = 1;', 60);
  assert.ok(!out.includes('**'), out);
  assert.ok(!out.includes('```'), out);
  assert.ok(out.includes('const'));
});
