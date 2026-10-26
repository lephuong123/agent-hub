import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { formatCost, formatElapsed, formatTokens, toolSummary, truncateLines } from './format.js';

test('toolSummary', () => {
  const cwd = path.resolve('/proj');
  assert.equal(toolSummary('Bash', { command: 'npm test' }), 'npm test');
  assert.equal(toolSummary('Read', { file_path: path.join(cwd, 'src', 'a.ts') }, cwd), path.join('src', 'a.ts'));
  assert.equal(toolSummary('Edit', { file_path: path.resolve('/other/b.ts') }, cwd), path.resolve('/other/b.ts'));
  assert.equal(toolSummary('Grep', { pattern: 'foo' }), '"foo"');
  assert.equal(toolSummary('Grep', { pattern: 'foo', path: path.join(cwd, 'src') }, cwd), '"foo" in src');
  assert.equal(toolSummary('Glob', { pattern: '**/*.ts' }), '**/*.ts');
  assert.equal(toolSummary('WebFetch', { url: 'https://x.dev' }), 'https://x.dev');
  assert.equal(toolSummary('WebSearch', { query: 'ink 6' }), 'ink 6');
  assert.equal(toolSummary('Agent', { description: 'Find files', prompt: '...' }), 'Find files');
  assert.equal(toolSummary('TodoWrite', { todos: [{}, {}, {}] }), '3 todos');
  assert.equal(toolSummary('mcp__github__get_issue', { id: 1 }), 'github - get_issue ({"id":1})');
  assert.equal(toolSummary('mcp__srv__noargs', {}), 'srv - noargs');
  const long = toolSummary('Custom', { text: 'x'.repeat(200) });
  assert.equal(long.length, 80);
  assert.ok(long.endsWith('…'));
  assert.equal(toolSummary('Custom', {}), '');
});

test('formatTokens', () => {
  assert.equal(formatTokens(999), '999');
  assert.equal(formatTokens(1000), '1k');
  assert.equal(formatTokens(1234), '1.2k');
  assert.equal(formatTokens(45_600), '46k');
  assert.equal(formatTokens(2_500_000), '2.5m');
});

test('formatCost', () => {
  assert.equal(formatCost(0), '$0.00');
  assert.equal(formatCost(0.0042), '$0.0042');
  assert.equal(formatCost(1.234), '$1.23');
});

test('formatElapsed', () => {
  assert.equal(formatElapsed(12_400), '12s');
  assert.equal(formatElapsed(63_000), '1m 3s');
  assert.equal(formatElapsed(3_723_000), '1h 2m');
});

test('truncateLines', () => {
  assert.deepEqual(truncateLines('a\nb', 3), { text: 'a\nb', hidden: 0 });
  assert.deepEqual(truncateLines('a\nb\nc\nd\n', 2), { text: 'a\nb', hidden: 2 });
});
