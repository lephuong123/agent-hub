import React from 'react';
import { render } from 'ink';
import { parseArgs } from 'node:util';
import path from 'node:path';
import type { PermissionMode } from './types.js';
import { HarnessSession } from './core/session.js';
import { App } from './App.js';
import { InlineApp } from './InlineApp.js';
import { enableMouse, MouseStdin } from './ui/term/mouse.js';

const HELP = `cfharness — Claude Code-style terminal UI on the Claude Agent SDK

Usage: cfharness [options] [prompt]

  -m, --model <name>      model id or alias (e.g. opus, sonnet)
  -r, --resume <id>       resume a previous session id
  -C, --cwd <dir>         working directory (default: current)
      --mode <mode>       default | acceptEdits | plan
      --dangerously-skip-permissions
                          start in bypassPermissions (no permission prompts)
      --inline            classic UI: print into the terminal's scrollback
                          instead of the fullscreen view
      --no-mouse          fullscreen without mouse capture (keeps native
                          text selection; scroll with PgUp/PgDn)
  -h, --help              show this help

Environment: CFH_NO_MOUSE=1 is the same as --no-mouse; CFH_SCROLL_SPEED sets
rows per wheel step (default 3).
`;

const { values, positionals } = parseArgs({
  allowPositionals: true,
  allowNegative: true,
  options: {
    model: { type: 'string', short: 'm' },
    resume: { type: 'string', short: 'r' },
    cwd: { type: 'string', short: 'C' },
    mode: { type: 'string' },
    'dangerously-skip-permissions': { type: 'boolean' },
    inline: { type: 'boolean' },
    mouse: { type: 'boolean', default: !process.env.CFH_NO_MOUSE },
    help: { type: 'boolean', short: 'h' },
  },
});

if (values.help) {
  process.stdout.write(HELP);
  process.exit(0);
}

const allowBypass = !!values['dangerously-skip-permissions'];
const modes: PermissionMode[] = ['default', 'acceptEdits', 'plan'];
if (values.mode && !modes.includes(values.mode as PermissionMode)) {
  process.stderr.write(`Unknown --mode "${values.mode}". Use one of: ${modes.join(', ')}\n`);
  process.exit(1);
}

const session = new HarnessSession({
  cwd: path.resolve(values.cwd ?? process.cwd()),
  model: values.model,
  resume: values.resume,
  permissionMode: allowBypass ? 'bypassPermissions' : (values.mode as PermissionMode | undefined),
  allowBypass,
});
session.start();
if (positionals.length) session.send(positionals.join(' '));

const tty = process.stdin.isTTY && process.stdout.isTTY;
if (values.inline || !tty) {
  const app = render(<InlineApp session={session} allowBypass={allowBypass} />, { exitOnCtrlC: false });
  await app.waitUntilExit();
} else {
  const mouseStdin = values.mouse ? new MouseStdin(process.stdin) : null;
  const disableMouse = mouseStdin ? enableMouse(process.stdout) : () => {};
  const app = render(<App session={session} allowBypass={allowBypass} mouse={mouseStdin} />, {
    exitOnCtrlC: false,
    alternateScreen: true,
    incrementalRendering: true,
    ...(mouseStdin ? { stdin: mouseStdin } : {}),
  });
  try {
    await app.waitUntilExit();
  } finally {
    disableMouse();
  }
}

// The fullscreen view leaves nothing behind on exit; say how to get back.
const id = session.getState().status.sessionId;
if (id) process.stdout.write(`Resume this session with: cfharness --resume ${id}\n`);
process.exit(0);
