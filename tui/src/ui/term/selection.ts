import sliceAnsi from 'slice-ansi';
import stringWidth from 'string-width';

export interface Cell {
  x: number;
  y: number;
}

/** Column span [from, to) selected on each row, for a stream selection between two cells (like a terminal's). */
export function selectedSpans(a: Cell, b: Cell, columns: number): Map<number, [number, number]> {
  const [start, end] = a.y < b.y || (a.y === b.y && a.x <= b.x) ? [a, b] : [b, a];
  const spans = new Map<number, [number, number]>();
  for (let y = start.y; y <= end.y; y++) {
    const from = y === start.y ? start.x : 0;
    const to = y === end.y ? end.x + 1 : columns;
    if (to > from) spans.set(y, [from, Math.min(to, columns)]);
  }
  return spans;
}

const ANSI = /\x1b\[[0-9;:?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
export const stripAnsi = (s: string) => s.replace(ANSI, '');

/** The selected text of a frame: each row's span, trailing blanks and the scrollbar column dropped. */
export function selectedText(frame: string, spans: Map<number, [number, number]>, columns: number): string {
  const lines = frame.split('\n');
  const out: string[] = [];
  for (const [y, [from, to]] of [...spans].sort((p, q) => p[0] - q[0])) {
    let text = stripAnsi(sliceAnsi(lines[y] ?? '', from, to)).trimEnd();
    if (to >= columns && /\s[│┃]$/.test(text)) text = text.slice(0, -1).trimEnd();
    out.push(text);
  }
  while (out.length && out[out.length - 1] === '') out.pop();
  return out.join('\n');
}

/** `line` with columns [from, to) shown inverted (their own colors dropped), padded so short rows still show the span. */
export function highlightLine(line: string, from: number, to: number): string {
  const width = stringWidth(line);
  const padded = width < to ? line + ' '.repeat(to - width) : line;
  const mid = stripAnsi(sliceAnsi(padded, from, to));
  return `${sliceAnsi(padded, 0, from)}\x1b[0m\x1b[7m${mid}\x1b[0m${sliceAnsi(padded, to)}`;
}

const SAVE = '\x1b7';
const RESTORE = '\x1b8';
const row = (y: number, content: string) => `\x1b[${y + 1};1H\x1b[0m\x1b[2K${content}\x1b[0m`;

/**
 * Drag selection drawn over Ink's frame. Ink does not know about it, so after every frame (and on
 * every change) the selected rows are repainted on top, and rows that leave the selection are put
 * back the way Ink drew them. The cursor is saved and restored around each paint so Ink's own
 * incremental updates still land where it expects.
 */
export class Selection {
  private anchor: Cell | null = null;
  private focus: Cell | null = null;
  private painted = new Set<number>();

  constructor(
    private readonly frame: () => string,
    private readonly write: (s: string) => void,
    private readonly columns: () => number,
  ) {}

  get active(): boolean {
    return this.anchor != null && this.focus != null;
  }

  spans(): Map<number, [number, number]> {
    return this.anchor && this.focus ? selectedSpans(this.anchor, this.focus, this.columns()) : new Map();
  }

  set(anchor: Cell, focus: Cell): void {
    this.anchor = anchor;
    this.focus = focus;
    this.paint();
  }

  clear(): void {
    if (!this.anchor && !this.painted.size) return;
    this.anchor = this.focus = null;
    this.paint();
  }

  text(): string {
    return selectedText(this.frame(), this.spans(), this.columns());
  }

  /** Redraw the overlay: call after Ink writes a frame, since that may have overwritten it. */
  paint = (): void => {
    const spans = this.spans();
    if (!spans.size && !this.painted.size) return;
    const lines = this.frame().split('\n');
    let out = '';
    for (const y of this.painted) if (!spans.has(y)) out += row(y, lines[y] ?? '');
    for (const [y, [from, to]] of spans) out += row(y, highlightLine(lines[y] ?? '', from, to));
    this.painted = new Set(spans.keys());
    if (out) this.write(SAVE + out + RESTORE);
  };
}
