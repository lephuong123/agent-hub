import React, { useEffect, useState } from 'react';
import { Box, render, Text } from 'ink';
import type { Entry, PermissionRequest, SessionStatus } from '../../types.js';
import { PermissionDialog, Spinner, StatusBar, Transcript, Welcome } from './index.js';

const cwd = process.cwd();

const assistantMd = `I'll add a \`retry\` helper. Here's the **plan**:

1. Wrap the fetch in a loop
2. Back off exponentially

\`\`\`ts
export async function retry<T>(fn: () => Promise<T>, n = 3): Promise<T> {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) { if (i >= n) throw e; }
  }
}
\`\`\`

| Attempt | Delay |
|---------|-------|
| 1       | 100ms |
| 2       | 200ms |

See [the docs](https://example.com/retry) for more.`;

const entries: Entry[] = [
  { kind: 'user', id: 'u1', text: 'Add a retry helper to src/net.ts and run the tests' },
  { kind: 'thinking', id: 'th1', streaming: false, text: 'The user wants a retry helper.\nI should look at net.ts first.\nThen edit it.\nThen run tests.\nAnd update todos.' },
  { kind: 'assistant', id: 'a1', streaming: false, parentToolUseId: null, text: assistantMd },
  {
    kind: 'tool', id: 'tw', name: 'TodoWrite', status: 'done', parentToolUseId: null, result: 'ok',
    input: { todos: [
      { content: 'Read src/net.ts', status: 'completed' },
      { content: 'Add retry helper', status: 'in_progress' },
      { content: 'Run tests', status: 'pending' },
    ] },
  },
  {
    kind: 'tool', id: 'e1', name: 'Edit', status: 'done', parentToolUseId: null, result: 'ok',
    input: { file_path: 'src/net.ts', old_string: 'import x from "y";\nexport const a = 1;', new_string: 'import x from "y";\nexport const a = 2;\nexport { retry } from "./retry.js";' },
  },
  {
    kind: 'tool', id: 'b1', name: 'Bash', status: 'done', parentToolUseId: null, input: { command: 'npm test' },
    result: '> test\n> tsx --test\n✔ retry works (3ms)\n✔ net ok (1ms)\nℹ tests 2\nℹ pass 2\nℹ fail 0',
  },
  { kind: 'tool', id: 'r1', name: 'Read', status: 'done', parentToolUseId: null, input: { file_path: 'src/retry.ts' }, result: 'a\nb\nc\nd' },
  { kind: 'tool', id: 'd1', name: 'Write', status: 'denied', parentToolUseId: null, input: { file_path: 'README.md', content: '# hi' } },
  { kind: 'system', id: 's1', level: 'error', text: 'API error: overloaded (529), retrying in 2s' },
  { kind: 'tool', id: 'g1', name: 'Grep', status: 'running', parentToolUseId: null, input: { pattern: 'fetch\\(', path: 'src' } },
  { kind: 'assistant', id: 'a2', streaming: true, parentToolUseId: null, text: 'Searching for callers:\n\n```ts\nfetch(' },
];

const status: SessionStatus = {
  busy: true, activity: 'Searching', turnStartedAt: Date.now() - 12_000, model: 'claude-opus-4', sessionId: '3f9a1c2e-7b4d-4e8f',
  cwd, permissionMode: 'default', totalCostUsd: 0.01234, inputTokens: 15320, outputTokens: 1210, turnOutputTokens: 1234,
};

const request: PermissionRequest = {
  id: 'p1', toolName: 'Bash', input: { command: 'rm -rf node_modules && npm install', description: 'Reinstall dependencies' },
  reason: 'Command modifies files outside the allow list', canAllowAlways: true,
};

function Demo() {
  const [showPerm, setShowPerm] = useState(false);
  const [decision, setDecision] = useState<string>();
  useEffect(() => {
    const t = setTimeout(() => setShowPerm(true), 1500);
    return () => clearTimeout(t);
  }, []);
  return (
    <Box flexDirection="column">
      <Transcript entries={entries} header={<Welcome cwd={cwd} model={status.model} />} />
      <Box marginTop={1}>
        <Spinner activity={status.activity} startedAt={status.turnStartedAt} tokens={status.turnOutputTokens} />
      </Box>
      {showPerm && !decision && <PermissionDialog request={request} onDecision={setDecision} />}
      {decision && <Text>Decision: {decision}</Text>}
      <StatusBar status={status} />
    </Box>
  );
}

const app = render(<Demo />);
const ms = Number(process.env.CFH_DEMO_MS ?? 0);
if (ms > 0) setTimeout(() => app.unmount(), ms);
