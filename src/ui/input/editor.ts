// Pure multiline text-buffer model. Cursor is a UTF-16 offset that always sits on a grapheme boundary.

export interface EditorState {
  readonly value: string;
  readonly cursor: number;
  /** Column (in graphemes) to aim for while moving up/down. */
  readonly goalCol?: number;
}

export const EMPTY: EditorState = { value: '', cursor: 0 };

const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
const WORD = /[\p{L}\p{N}\p{M}_]/u;

const make = (value: string, cursor: number): EditorState => ({ value, cursor });

export const lineStartOf = (v: string, i: number): number => (i <= 0 ? 0 : v.lastIndexOf('\n', i - 1) + 1);
export const lineEndOf = (v: string, i: number): number => {
  const j = v.indexOf('\n', i);
  return j < 0 ? v.length : j;
};

/** Grapheme boundary offsets of v[from, to), including `to`. `from` must be a boundary. */
function boundaries(v: string, from: number, to: number): number[] {
  const out: number[] = [];
  for (const s of segmenter.segment(v.slice(from, to))) out.push(from + s.index);
  out.push(to);
  return out;
}

export function prevBoundary(v: string, i: number): number {
  if (i <= 0) return 0;
  if (v[i - 1] === '\n') return i - 1;
  const b = boundaries(v, lineStartOf(v, i), i);
  return b[b.length - 2] ?? 0;
}

export function nextBoundary(v: string, i: number): number {
  if (i >= v.length) return v.length;
  if (v[i] === '\n') return i + 1;
  const first = segmenter.segment(v.slice(i, lineEndOf(v, i)))[Symbol.iterator]().next();
  return first.done ? i + 1 : i + first.value.segment.length;
}

/** Move i forward to the nearest grapheme boundary (no-op if already on one). */
function snap(v: string, i: number): number {
  const ls = lineStartOf(v, i);
  for (const b of boundaries(v, ls, lineEndOf(v, i))) if (b >= i) return b;
  return i;
}

/** Normalise line endings and drop control characters other than \n and \t. */
export const cleanText = (t: string): string =>
  t.replace(/\r\n?/g, '\n').replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');

export const fromText = (value: string, cursor = value.length): EditorState =>
  make(value, Math.max(0, Math.min(cursor, value.length)));

export function insert(s: EditorState, text: string): EditorState {
  const t = cleanText(text);
  if (!t) return s;
  const before = (s.value.slice(0, s.cursor) + t).normalize('NFC');
  const value = before + s.value.slice(s.cursor);
  return make(value, snap(value, before.length));
}

/** Apply a raw multi-char chunk: embedded DEL/BS act as backspace (IME re-composition), the rest is inserted. */
export function applyChunk(s: EditorState, chunk: string): EditorState {
  let st = s;
  for (const part of chunk.split(/([\x7f\b])/)) {
    if (part === '\x7f' || part === '\b') st = backspace(st);
    else if (part) st = insert(st, part);
  }
  return st;
}

export const newline = (s: EditorState): EditorState => insert(s, '\n');

const remove = (s: EditorState, from: number, to: number): EditorState =>
  from === to ? make(s.value, s.cursor) : make(s.value.slice(0, from) + s.value.slice(to), from);

export const backspace = (s: EditorState): EditorState => remove(s, prevBoundary(s.value, s.cursor), s.cursor);
export const del = (s: EditorState): EditorState => remove(s, s.cursor, nextBoundary(s.value, s.cursor));
export const left = (s: EditorState): EditorState => make(s.value, prevBoundary(s.value, s.cursor));
export const right = (s: EditorState): EditorState => make(s.value, nextBoundary(s.value, s.cursor));
export const lineStart = (s: EditorState): EditorState => make(s.value, lineStartOf(s.value, s.cursor));
export const lineEnd = (s: EditorState): EditorState => make(s.value, lineEndOf(s.value, s.cursor));
export const bufferStart = (s: EditorState): EditorState => make(s.value, 0);
export const bufferEnd = (s: EditorState): EditorState => make(s.value, s.value.length);

const isWordAt = (v: string, from: number, to: number) => WORD.test(v.slice(from, to));

function wordLeftOffset(v: string, i: number): number {
  while (i > 0 && !isWordAt(v, prevBoundary(v, i), i)) i = prevBoundary(v, i);
  while (i > 0 && isWordAt(v, prevBoundary(v, i), i)) i = prevBoundary(v, i);
  return i;
}

function wordRightOffset(v: string, i: number): number {
  while (i < v.length && !isWordAt(v, i, nextBoundary(v, i))) i = nextBoundary(v, i);
  while (i < v.length && isWordAt(v, i, nextBoundary(v, i))) i = nextBoundary(v, i);
  return i;
}

export const wordLeft = (s: EditorState): EditorState => make(s.value, wordLeftOffset(s.value, s.cursor));
export const wordRight = (s: EditorState): EditorState => make(s.value, wordRightOffset(s.value, s.cursor));
export const deleteWordBack = (s: EditorState): EditorState => remove(s, wordLeftOffset(s.value, s.cursor), s.cursor);
export const deleteWordForward = (s: EditorState): EditorState => remove(s, s.cursor, wordRightOffset(s.value, s.cursor));

/** Ctrl+U: delete to line start; at line start, join with the previous line. */
export function deleteToLineStart(s: EditorState): EditorState {
  const ls = lineStartOf(s.value, s.cursor);
  return ls === s.cursor ? remove(s, Math.max(0, ls - 1), s.cursor) : remove(s, ls, s.cursor);
}

/** Ctrl+K: delete to line end; at line end, join with the next line. */
export function deleteToLineEnd(s: EditorState): EditorState {
  const le = lineEndOf(s.value, s.cursor);
  return le === s.cursor ? remove(s, s.cursor, Math.min(s.value.length, le + 1)) : remove(s, s.cursor, le);
}

export interface CursorPos {
  line: number;
  /** Column in graphemes. */
  col: number;
}

export function position(s: EditorState): CursorPos {
  const v = s.value;
  let line = 0;
  for (let i = v.indexOf('\n'); i >= 0 && i < s.cursor; i = v.indexOf('\n', i + 1)) line++;
  const ls = lineStartOf(v, s.cursor);
  return { line, col: boundaries(v, ls, s.cursor).length - 1 };
}

export const isOnFirstLine = (s: EditorState): boolean => lineStartOf(s.value, s.cursor) === 0;
export const isOnLastLine = (s: EditorState): boolean => s.value.indexOf('\n', s.cursor) < 0;

function offsetAtCol(v: string, lineStart: number, col: number): number {
  const b = boundaries(v, lineStart, lineEndOf(v, lineStart));
  return b[Math.min(col, b.length - 1)]!;
}

/** Returns `s` unchanged (same object) when already on the first line. */
export function up(s: EditorState): EditorState {
  if (isOnFirstLine(s)) return s;
  const goalCol = s.goalCol ?? position(s).col;
  const prevStart = lineStartOf(s.value, lineStartOf(s.value, s.cursor) - 1);
  return { value: s.value, cursor: offsetAtCol(s.value, prevStart, goalCol), goalCol };
}

/** Returns `s` unchanged (same object) when already on the last line. */
export function down(s: EditorState): EditorState {
  if (isOnLastLine(s)) return s;
  const goalCol = s.goalCol ?? position(s).col;
  const nextStart = lineEndOf(s.value, s.cursor) + 1;
  return { value: s.value, cursor: offsetAtCol(s.value, nextStart, goalCol), goalCol };
}

/** Grapheme under the cursor ('' at end of line / buffer). */
export function graphemeAt(s: EditorState): string {
  if (s.cursor >= s.value.length || s.value[s.cursor] === '\n') return '';
  return s.value.slice(s.cursor, nextBoundary(s.value, s.cursor));
}

// ---------------------------------------------------------------- history

export interface HistoryState {
  /** Oldest first. */
  readonly entries: readonly string[];
  /** entries.length means "editing the draft". */
  readonly index: number;
  readonly draft: string;
}

export const createHistory = (entries: readonly string[] = []): HistoryState => ({
  entries: [...entries],
  index: entries.length,
  draft: '',
});

export interface HistoryMove {
  history: HistoryState;
  value: string;
}

export function historyPrev(h: HistoryState, current: string): HistoryMove | null {
  if (h.index <= 0 || h.entries.length === 0) return null;
  const draft = h.index >= h.entries.length ? current : h.draft;
  const index = Math.min(h.index, h.entries.length) - 1;
  return { history: { ...h, index, draft }, value: h.entries[index]! };
}

export function historyNext(h: HistoryState): HistoryMove | null {
  if (h.index >= h.entries.length) return null;
  const index = h.index + 1;
  return { history: { ...h, index }, value: index === h.entries.length ? h.draft : h.entries[index]! };
}

export function historyPush(h: HistoryState, text: string): HistoryState {
  const entries = text && h.entries[h.entries.length - 1] !== text ? [...h.entries, text] : [...h.entries];
  return { entries, index: entries.length, draft: '' };
}

/** Editor state for a recalled entry: Up lands on the end of its first line, Down on the very end, so repeated presses keep walking history. */
export const recalled = (value: string, direction: 'prev' | 'next'): EditorState =>
  fromText(value, direction === 'prev' ? lineEndOf(value, 0) : value.length);
