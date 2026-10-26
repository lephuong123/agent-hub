import { PassThrough } from 'node:stream';

// SGR mouse reporting (xterm 1006): ESC [ < b ; x ; y (M = press/motion | m = release).
// Ink 8 drops these sequences before useInput sees them, so they are split off here,
// in a stdin proxy, before Ink reads the stream.

export type MouseAction = 'press' | 'release' | 'drag' | 'move' | 'wheelUp' | 'wheelDown';
export type MouseButton = 'left' | 'middle' | 'right' | 'none';

export interface TermMouseEvent {
  action: MouseAction;
  button: MouseButton;
  /** 0-based cell coordinates. */
  x: number;
  y: number;
  shift: boolean;
  meta: boolean;
  ctrl: boolean;
}

const SGR = /\x1b\[<(\d+);(\d+);(\d+)([Mm])/g;
/** A sequence cut at the end of a chunk; held back until the rest arrives. */
const PARTIAL = /\x1b(\[(<[\d;]*)?)?$/;

const BUTTONS: MouseButton[] = ['left', 'middle', 'right', 'none'];

export function decodeMouse(code: number, x: number, y: number, release: boolean): TermMouseEvent {
  const mods = { shift: !!(code & 4), meta: !!(code & 8), ctrl: !!(code & 16) };
  const pos = { x: x - 1, y: y - 1 };
  if (code & 64) {
    const action: MouseAction = (code & 1) === 0 ? 'wheelUp' : 'wheelDown';
    return { action, button: 'none', ...pos, ...mods };
  }
  const button = BUTTONS[code & 3]!;
  const action: MouseAction = release ? 'release' : code & 32 ? (button === 'none' ? 'move' : 'drag') : 'press';
  return { action, button, ...pos, ...mods };
}

/**
 * Split mouse reports out of a stdin chunk. `rest` is a trailing incomplete escape sequence
 * that may be the start of a mouse report; prepend it to the next chunk (or flush it as text
 * when nothing follows, since a lone ESC is the Escape key).
 */
export function extractMouse(chunk: string): { text: string; events: TermMouseEvent[]; rest: string } {
  const events: TermMouseEvent[] = [];
  let text = chunk.replace(SGR, (_, b: string, x: string, y: string, kind: string) => {
    events.push(decodeMouse(Number(b), Number(x), Number(y), kind === 'm'));
    return '';
  });
  let rest = '';
  const partial = PARTIAL.exec(text);
  if (partial) {
    rest = partial[0];
    text = text.slice(0, partial.index);
  }
  return { text, events, rest };
}

export const MOUSE_ON = '\x1b[?1000h\x1b[?1002h\x1b[?1006h';
export const MOUSE_OFF = '\x1b[?1006l\x1b[?1002l\x1b[?1000l';

type RawStdin = NodeJS.ReadStream & { setRawMode(mode: boolean): unknown };

/**
 * A stdin stand-in for Ink: forwards keystrokes, raw mode and ref/unref to the real stdin,
 * and reports mouse events to `onMouse` listeners instead.
 */
export class MouseStdin extends PassThrough {
  readonly isTTY = true;
  private mouseListeners = new Set<(e: TermMouseEvent) => void>();
  private pending = '';
  private flushTimer: NodeJS.Timeout | undefined;
  private attached = false;

  constructor(private readonly real: RawStdin) {
    super();
  }

  setRawMode(mode: boolean): this {
    this.real.setRawMode(mode);
    if (mode) this.attach();
    else if (this.attached) {
      this.attached = false;
      this.real.off('data', this.onData);
      this.real.pause();
    }
    return this;
  }

  private attach(): void {
    if (this.attached) return;
    this.attached = true;
    this.real.setEncoding('utf8');
    this.real.on('data', this.onData);
  }

  ref(): this {
    this.real.ref();
    return this;
  }

  unref(): this {
    this.real.unref();
    return this;
  }

  onMouse(listener: (e: TermMouseEvent) => void): () => void {
    this.mouseListeners.add(listener);
    this.attach();
    return () => this.mouseListeners.delete(listener);
  }

  private onData = (data: string | Buffer): void => {
    clearTimeout(this.flushTimer);
    const { text, events, rest } = extractMouse(this.pending + String(data));
    this.pending = rest;
    if (text) this.write(text);
    for (const e of events) for (const l of [...this.mouseListeners]) l(e);
    if (rest) this.flushTimer = setTimeout(this.flush, 25);
  };

  private flush = (): void => {
    const rest = this.pending;
    this.pending = '';
    if (rest) this.write(rest);
  };
}

/** Turn mouse reporting on, and make sure it is turned off however the process ends. */
export function enableMouse(stdout: NodeJS.WriteStream): () => void {
  let on = true;
  const off = () => {
    if (!on) return;
    on = false;
    stdout.write(MOUSE_OFF);
  };
  stdout.write(MOUSE_ON);
  process.once('exit', off);
  return () => {
    process.off('exit', off);
    off();
  };
}
