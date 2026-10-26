import React, { useCallback, useState, useSyncExternalStore } from 'react';
import { Box, useApp } from 'ink';
import type { Session } from './types.js';
import { nextPermissionMode, runLocalCommand } from './core/localCommands.js';
import { PromptInput } from './ui/input/PromptInput.js';
import { PermissionDialog, Spinner, StatusBar, Transcript, Welcome } from './ui/render/index.js';

export function App({ session, allowBypass = false }: { session: Session; allowBypass?: boolean }) {
  const { exit } = useApp();
  const state = useSyncExternalStore(session.subscribe.bind(session), () => session.getState());
  const { status, pendingPermission } = state;
  const [clearKey, setClearKey] = useState(0);
  const [history, setHistory] = useState<string[]>([]);
  const quit = useCallback(() => {
    void session.close().finally(() => exit());
  }, [session, exit]);


  const onSubmit = useCallback(
    (text: string) => {
      setHistory(h => [...h, text]);
      const m = /^\/(\S+)\s*([\s\S]*)$/.exec(text);
      if (m) {
        const [, name, args] = m;
        if (name === 'clear') setClearKey(k => k + 1);
        if (runLocalCommand(session, name!, args!.trim(), { exit: quit, allowBypass })) return;
      }
      session.send(text);
    },
    [session, quit, allowBypass],
  );

  const cycleMode = useCallback(() => {
    void session.setPermissionMode(nextPermissionMode(status.permissionMode, allowBypass));
  }, [session, status.permissionMode, allowBypass]);

  return (
    <Box flexDirection="column">
      <Transcript
        entries={state.entries}
        clearKey={clearKey}
        header={<Welcome cwd={status.cwd} model={status.model} />}
      />
      {status.busy && !pendingPermission && (
        <Spinner
          activity={status.activity ?? 'Thinking'}
          startedAt={status.turnStartedAt}
          tokens={status.turnOutputTokens}
        />
      )}
      {pendingPermission && (
        <PermissionDialog
          key={pendingPermission.id}
          request={pendingPermission}
          onDecision={d => session.answerPermission(pendingPermission.id, d)}
        />
      )}
      <PromptInput
        onSubmit={onSubmit}
        commands={state.slashCommands}
        disabled={!!pendingPermission}
        busy={status.busy}
        permissionMode={status.permissionMode}
        onInterrupt={() => void session.interrupt()}
        onCyclePermissionMode={cycleMode}
        onExit={quit}
        history={history}
      />
      <StatusBar status={status} />
    </Box>
  );
}
