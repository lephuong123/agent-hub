import React, { useCallback, useState } from 'react';
import { Box } from 'ink';
import type { Session } from './types.js';
import { useHarness } from './useHarness.js';
import { PromptInput } from './ui/input/PromptInput.js';
import { PermissionDialog, Spinner, StatusBar, Transcript, Welcome } from './ui/render/index.js';

/** The scrollback UI (--inline): finished items go to the terminal's own history and are never redrawn. */
export function InlineApp({ session, allowBypass = false }: { session: Session; allowBypass?: boolean }) {
  const [clearKey, setClearKey] = useState(0);
  const onClear = useCallback(() => setClearKey((k) => k + 1), []);
  const { state, quit, submit, cycleMode, background } = useHarness(session, allowBypass, onClear);
  const { status, pendingPermission } = state;

  return (
    <Box flexDirection="column">
      <Transcript entries={state.entries} tasks={state.tasks} clearKey={clearKey} header={<Welcome cwd={status.cwd} model={status.model} />} />
      {status.busy && !pendingPermission && (
        <Spinner activity={status.activity ?? 'Thinking'} startedAt={status.turnStartedAt} tokens={status.turnOutputTokens} />
      )}
      {pendingPermission && (
        <PermissionDialog
          key={pendingPermission.id}
          request={pendingPermission}
          onDecision={(d) => session.answerPermission(pendingPermission.id, d)}
        />
      )}
      <PromptInput
        onSubmit={submit}
        commands={state.slashCommands}
        disabled={!!pendingPermission}
        busy={status.busy}
        permissionMode={status.permissionMode}
        onInterrupt={() => void session.interrupt()}
        onCyclePermissionMode={cycleMode}
        onBackground={background}
        onExit={quit}
      />
      <StatusBar status={status} />
    </Box>
  );
}
