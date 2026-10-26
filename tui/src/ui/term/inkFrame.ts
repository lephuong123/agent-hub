import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// Ink keeps the last frame it drew (`lastOutput`, ANSI text, rows joined by "\n") on its internal
// instance, which is registered per stdout in build/instances.js. That module is not in Ink's
// package exports, so it is loaded by file path; ink is pinned to an exact version for this, and
// selection.test.tsx fails if a new version moves it.

interface InkInstance {
  lastOutput?: string;
}

let instances: WeakMap<object, InkInstance> | null | undefined;

async function load(): Promise<WeakMap<object, InkInstance> | null> {
  if (instances !== undefined) return instances;
  try {
    const entry = createRequire(import.meta.url).resolve('ink');
    const mod = (await import(pathToFileURL(path.join(path.dirname(entry), 'instances.js')).href)) as {
      default?: WeakMap<object, InkInstance>;
    };
    instances = mod.default instanceof WeakMap ? mod.default : null;
  } catch {
    instances = null;
  }
  return instances;
}

/** Returns a reader for the frame Ink last drew to `stdout`, or null if Ink's internals are not where expected. */
export async function inkFrameReader(stdout: object): Promise<(() => string) | null> {
  const map = await load();
  if (!map) return null;
  return () => map.get(stdout)?.lastOutput ?? '';
}
