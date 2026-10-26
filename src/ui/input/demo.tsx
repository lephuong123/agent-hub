import React, { useState } from 'react';
import { Box, Static, Text, render, useApp } from 'ink';
import type { PermissionMode, SlashCommand } from '../../types.js';
import { PromptInput } from './PromptInput.js';

const COMMANDS: SlashCommand[] = [
  { name: 'help', description: 'Show help and available commands', source: 'local' },
  { name: 'clear', description: 'Clear conversation history', source: 'local' },
  { name: 'compact', description: 'Summarise the conversation to free context', argumentHint: '[instructions]', source: 'remote' },
  { name: 'config', description: 'Open config panel', source: 'local' },
  { name: 'cost', description: 'Show the total cost of this session', source: 'local' },
  { name: 'model', description: 'Set the AI model', argumentHint: '[model]', source: 'local' },
  { name: 'busy', description: 'Demo: pretend a turn is running for 5s (Esc interrupts)', source: 'local' },
  { name: 'exit', description: 'Exit the demo', source: 'local' },
];
const MODES: PermissionMode[] = ['default', 'acceptEdits', 'plan', 'bypassPermissions'];

function Demo() {
  const { exit } = useApp();
  const [items, setItems] = useState<{ id: number; text: string }[]>([]);
  const [mode, setMode] = useState<PermissionMode>('default');
  const [busy, setBusy] = useState(false);
  const log = (text: string) => setItems((xs) => [...xs, { id: xs.length, text }]);

  return (
    <>
      <Static items={items}>
        {(it) => (
          <Box key={it.id} marginBottom={1}>
            <Text color="gray">{'> '}</Text>
            <Text>{it.text}</Text>
          </Box>
        )}
      </Static>
      {busy ? <Text color="#d77757">✻ Working… (esc to interrupt)</Text> : null}
      <PromptInput
        commands={COMMANDS}
        busy={busy}
        permissionMode={mode}
        history={['earlier prompt', 'a\nmultiline\nprompt']}
        onSubmit={(text) => {
          log(text);
          if (text === '/exit') exit();
          if (text === '/busy') {
            setBusy(true);
            setTimeout(() => setBusy(false), 5000);
          }
        }}
        onInterrupt={() => {
          setBusy(false);
          log('[interrupted]');
        }}
        onCyclePermissionMode={() => setMode((m) => MODES[(MODES.indexOf(m) + 1) % MODES.length]!)}
        onExit={() => exit()}
      />
    </>
  );
}

render(<Demo />, { exitOnCtrlC: false });
