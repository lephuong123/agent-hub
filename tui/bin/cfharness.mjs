#!/usr/bin/env node
// Launcher: runs the TypeScript entry through tsx so no build step is needed.
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const tsxCli = createRequire(import.meta.url).resolve('tsx/cli');
const r = spawnSync(process.execPath, [tsxCli, path.join(root, 'src', 'cli.tsx'), ...process.argv.slice(2)], { stdio: 'inherit' });
process.exit(r.status ?? 1);
