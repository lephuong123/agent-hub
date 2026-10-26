import { useCallback, useSyncExternalStore } from 'react';
import { useApp } from 'ink';
import type { Session } from './types.js';
import { nextPermissionMode, runLocalCommand } from './core/localCommands.js';

/** Session state plus the prompt actions both UIs (fullscreen and inline) share. */
export function useHarness(session: Session, allowBypass: boolean, onClear: () => void) {
  const { exit } = useApp();
  const state = useSyncExternalStore(session.subscribe.bind(session), () => session.getState());

  const quit = useCallback(() => {
    void session.close().finally(() => exit());
  }, [session, exit]);

  const submit = useCallback(
    (text: string) => {
      const m = /^\/(\S+)\s*([\s\S]*)$/.exec(text);
      if (m) {
        const [, name, args] = m;
        if (name === 'clear') onClear();
        if (runLocalCommand(session, name!, args!.trim(), { exit: quit, allowBypass })) return;
      }
      session.send(text);
    },
    [session, quit, allowBypass, onClear],
  );

  const mode = state.status.permissionMode;
  const cycleMode = useCallback(() => {
    void session.setPermissionMode(nextPermissionMode(mode, allowBypass));
  }, [session, mode, allowBypass]);

  const background = useCallback(() => {
    session.backgroundTasks().catch((e: unknown) => session.notify(`Could not background: ${e instanceof Error ? e.message : String(e)}`, 'warn'));
  }, [session]);

  return { state, quit, submit, cycleMode, background };
}
