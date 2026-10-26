import type { PermissionMode, Session, SlashCommand } from '../types.js';
import { formatCost, formatTokens } from './format.js';

export const PERMISSION_MODES: PermissionMode[] = ['default', 'acceptEdits', 'plan', 'bypassPermissions'];

export const LOCAL_COMMANDS: SlashCommand[] = [
  { name: 'help', description: 'Show available commands', source: 'local' },
  { name: 'clear', description: 'Clear the visible transcript', source: 'local' },
  { name: 'model', description: 'Show or switch the model', argumentHint: '[model]', source: 'local' },
  {
    name: 'mode',
    description: 'Cycle or set the permission mode',
    argumentHint: '[default|acceptEdits|plan]',
    source: 'local',
  },
  { name: 'cost', description: 'Show session cost and token usage', source: 'local' },
  { name: 'exit', description: 'Exit cfharness', source: 'local' },
];

export function nextPermissionMode(mode: PermissionMode, allowBypass = false): PermissionMode {
  const modes = allowBypass ? PERMISSION_MODES : PERMISSION_MODES.filter(m => m !== 'bypassPermissions');
  return modes[(modes.indexOf(mode) + 1) % modes.length]!;
}

const report = (session: Session, p: Promise<unknown>, ok: string) =>
  void p.then(
    () => session.notify(ok),
    (e: unknown) => session.notify(`Failed: ${e instanceof Error ? e.message : String(e)}`, 'error'),
  );

/** Returns true if `name` is a local command (and runs it). */
export function runLocalCommand(session: Session, name: string, args: string, ctx: { exit: () => void; allowBypass?: boolean }): boolean {
  const arg = args.trim();
  const { status, slashCommands } = session.getState();
  switch (name) {
    case 'help': {
      const width = Math.max(...slashCommands.map((c) => c.name.length + (c.argumentHint ? c.argumentHint.length + 1 : 0)));
      const lines = slashCommands.map((c) => {
        const head = c.argumentHint ? `${c.name} ${c.argumentHint}` : c.name;
        return `  /${head.padEnd(width)}  ${c.description}`;
      });
      session.notify(['Commands:', ...lines].join('\n'));
      return true;
    }
    case 'clear':
      session.clearTranscript();
      return true;
    case 'model':
      if (!arg) session.notify(`Model: ${status.model ?? '(default)'}`);
      else report(session, session.setModel(arg === 'default' ? undefined : arg), `Model set to ${arg}`);
      return true;
    case 'mode': {
      if (arg && !PERMISSION_MODES.includes(arg as PermissionMode)) {
        session.notify(`Unknown mode "${arg}". Use one of: ${PERMISSION_MODES.join(', ')}`, 'warn');
        return true;
      }
      const mode = (arg as PermissionMode) || nextPermissionMode(status.permissionMode, ctx.allowBypass);
      report(session, session.setPermissionMode(mode), `Permission mode: ${mode}`);
      return true;
    }
    case 'cost':
      session.notify(
        `Total cost: ${formatCost(status.totalCostUsd)}\n` +
          `Tokens: ${formatTokens(status.inputTokens)} in, ${formatTokens(status.outputTokens)} out`,
      );
      return true;
    case 'exit':
    case 'quit':
      ctx.exit();
      return true;
    default:
      return false;
  }
}
