import React from 'react';
import { render } from 'ink';
import { parseArgs } from 'node:util';
import path from 'node:path';
import type { PermissionMode } from './types.js';
import { HarnessSession } from './core/session.js';
import { App } from './App.js';

const HELP = `cfharness — Claude Code-style terminal UI on the Claude Agent SDK

Usage: cfharness [options] [prompt]

  -m, --model <name>      model id or alias (e.g. opus, sonnet)
  -r, --resume <id>       resume a previous session id
  -C, --cwd <dir>         working directory (default: current)
      --mode <mode>       default | acceptEdits | plan
      --dangerously-skip-permissions
                          start in bypassPermissions (no permission prompts)
  -h, --help              show this help
`;

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    model: { type: 'string', short: 'm' },
    resume: { type: 'string', short: 'r' },
    cwd: { type: 'string', short: 'C' },
    mode: { type: 'string' },
    'dangerously-skip-permissions': { type: 'boolean' },
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

const app = render(<App session={session} allowBypass={allowBypass} />, { exitOnCtrlC: false });
await app.waitUntilExit();
process.exit(0);
