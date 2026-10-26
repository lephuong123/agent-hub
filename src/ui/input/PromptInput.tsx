import React, { useCallback, useEffect, useReducer, useRef } from 'react';
import { Box, Text, useInput, useStdin, useStdout, type Key } from 'ink';
import type { PermissionMode, SlashCommand } from '../../types.js';
import * as E from './editor.js';
import { matchCommands, slashQuery } from './slash.js';
import { expandPastes, isLargePaste, pasteToken } from './paste.js';

export interface PromptInputProps {
  onSubmit(text: string): void;
  commands: SlashCommand[];
  /** True while a permission dialog has focus: all keys are ignored. */
  disabled?: boolean;
  busy: boolean;
  permissionMode: PermissionMode;
  onInterrupt(): void;
  onCyclePermissionMode(): void;
  onExit(): void;
  /** Initial history, oldest first. Submitted prompts are appended internally. */
  history?: string[];
}

const PLACEHOLDER = 'Try "how does <filepath> work?"';
const PASTE_START = '\x1b[200~';
const PASTE_END = '\x1b[201~';
const MENU_ROWS = 8;
const ESC_WINDOW = 1000;
const CTRL_C_WINDOW = 2000;

export const MODE_LABEL: Record<PermissionMode, { text: string; color: string } | null> = {
  default: null,
  acceptEdits: { text: '⏵⏵ accept edits on (shift+tab to cycle)', color: '#af87ff' },
  plan: { text: '⏸ plan mode on (shift+tab to cycle)', color: '#48968c' },
  bypassPermissions: { text: '⏵⏵ bypass permissions on (shift+tab to cycle)', color: '#ff6b80' },
};

const SHORTCUTS: [string, string][] = [
  ['/ for commands', 'esc to interrupt'],
  ['shift+tab to cycle modes', 'double esc to clear input'],
  ['\\⏎ or ctrl+j for newline', 'shift+⏎ / alt+⏎ for newline'],
  ['↑↓ for history', 'ctrl+c to clear · twice to exit'],
  ['ctrl+a/e line start/end', 'ctrl+w/u/k delete word/line'],
];

interface Model {
  ed: E.EditorState;
  hist: E.HistoryState;
  sel: number;
  menuOff: boolean;
  help: boolean;
  pastes: Map<string, string>;
  pasteSeq: number;
  /** Accumulated bracketed-paste text, or null when not inside a paste. */
  pasting: string | null;
  /** Raw sequence of the event currently being handled (lets us tell Backspace from Delete, Ctrl+J, etc). */
  raw: string;
  lastEsc: number;
  lastCtrlC: number;
  hint: string | null;
  hintTimer?: NodeJS.Timeout;
}

const isForwardDelete = (raw: string) => /^\x1b\[3(;\d+)?(:\d+)?~$/.test(raw);

export function PromptInput(props: PromptInputProps) {
  const { disabled = false } = props;
  const [, rerender] = useReducer((x: number) => x + 1, 0);
  const propsRef = useRef(props);
  propsRef.current = props;
  const modelRef = useRef<Model | null>(null);
  modelRef.current ??= {
    ed: E.EMPTY,
    hist: E.createHistory(props.history ?? []),
    sel: 0,
    menuOff: false,
    help: false,
    pastes: new Map(),
    pasteSeq: 0,
    pasting: null,
    raw: '',
    lastEsc: 0,
    lastCtrlC: 0,
    hint: null,
  };
  const m = modelRef.current;

  const menuFor = useCallback((model: Model, commands: SlashCommand[]) => {
    const q = slashQuery(model.ed.value);
    return q === null || model.menuOff ? [] : matchCommands(commands, q);
  }, []);

  const setEd = useCallback((next: E.EditorState) => {
    const model = modelRef.current!;
    if (next.value !== model.ed.value) {
      model.sel = 0;
      model.menuOff = false;
      if (next.value) model.help = false;
    }
    model.ed = next;
    rerender();
  }, []);

  const showHint = useCallback((text: string | null, ms = 0) => {
    const model = modelRef.current!;
    clearTimeout(model.hintTimer);
    model.hint = text;
    if (text && ms)
      model.hintTimer = setTimeout(() => {
        model.hint = null;
        rerender();
      }, ms);
    rerender();
  }, []);

  const insertText = useCallback(
    (text: string, isPaste: boolean) => {
      const model = modelRef.current!;
      const clean = E.cleanText(text);
      if (isLargePaste(clean)) {
        const token = pasteToken(++model.pasteSeq, clean);
        model.pastes.set(token, clean);
        setEd(E.insert(model.ed, token));
      } else setEd(isPaste ? E.insert(model.ed, clean) : E.applyChunk(model.ed, text));
    },
    [setEd],
  );

  const submit = useCallback(
    (text: string) => {
      const model = modelRef.current!;
      const expanded = expandPastes(text, model.pastes).trim();
      if (!expanded) return;
      model.hist = E.historyPush(model.hist, expanded);
      model.pastes.clear();
      setEd(E.EMPTY);
      propsRef.current.onSubmit(expanded);
    },
    [setEd],
  );

  // Raw listener: registered before useInput's, so it sees each event first. Handles bracketed paste.
  const { internal_eventEmitter: emitter, isRawModeSupported } = useStdin();
  useEffect(() => {
    const onRaw = (raw: string) => {
      const model = modelRef.current!;
      model.raw = raw;
      if (propsRef.current.disabled) return;
      if (raw === PASTE_START) model.pasting = '';
      else if (raw === PASTE_END) {
        const text = model.pasting ?? '';
        model.pasting = null;
        if (text) insertText(text, true);
      } else if (model.pasting !== null) model.pasting += raw;
    };
    emitter.on('input', onRaw);
    return () => {
      emitter.off('input', onRaw);
    };
  }, [emitter, insertText]);

  const { stdout } = useStdout();
  useEffect(() => {
    if (!stdout.isTTY) return;
    const off = () => stdout.write('\x1b[?2004l');
    stdout.write('\x1b[?2004h');
    process.once('exit', off);
    return () => {
      off();
      process.off('exit', off);
    };
  }, [stdout]);

  useEffect(() => () => clearTimeout(modelRef.current?.hintTimer), []);

  const handle = useCallback(
    (input: string, key: Key) => {
      const p = propsRef.current;
      const model = modelRef.current!;
      const raw = model.raw;
      if (model.pasting !== null || raw === PASTE_START || raw === PASTE_END) return;
      const now = Date.now();
      const ed = model.ed;

      if (key.ctrl && input === 'c') {
        model.lastEsc = 0;
        if (ed.value) {
          model.lastCtrlC = 0;
          setEd(E.EMPTY);
          showHint(null);
        } else if (now - model.lastCtrlC < CTRL_C_WINDOW) {
          model.lastCtrlC = 0;
          showHint(null);
          p.onExit();
        } else {
          model.lastCtrlC = now;
          showHint('Press Ctrl+C again to exit', CTRL_C_WINDOW);
        }
        return;
      }

      if (key.escape) {
        model.lastCtrlC = 0;
        if (p.busy) return p.onInterrupt();
        if (model.help) {
          model.help = false;
          return rerender();
        }
        if (menuFor(model, p.commands).length) {
          model.menuOff = true;
          return rerender();
        }
        if (!ed.value) return;
        if (now - model.lastEsc < ESC_WINDOW) {
          model.lastEsc = 0;
          setEd(E.EMPTY);
          showHint(null);
        } else {
          model.lastEsc = now;
          showHint('Esc again to clear', ESC_WINDOW);
        }
        return;
      }

      if (model.lastEsc || model.lastCtrlC) {
        model.lastEsc = model.lastCtrlC = 0;
        showHint(null);
      }

      if (key.tab && key.shift) return p.onCyclePermissionMode();

      const menu = menuFor(model, p.commands);
      if (menu.length) {
        if (key.upArrow || key.downArrow) {
          model.sel = (model.sel + (key.upArrow ? -1 : 1) + menu.length) % menu.length;
          return rerender();
        }
        const chosen = menu[Math.min(model.sel, menu.length - 1)]!;
        if (key.tab) return setEd(E.fromText(`/${chosen.name} `));
        if (key.return && !key.shift && !key.meta) return submit(`/${chosen.name}`);
      }

      if (raw === '\n' || (key.ctrl && input === 'j')) return setEd(E.newline(ed));
      if (key.return) {
        if (key.shift || key.meta) return setEd(E.newline(ed));
        if (ed.cursor > 0 && ed.value[ed.cursor - 1] === '\\') return setEd(E.newline(E.backspace(ed)));
        return submit(ed.value);
      }

      if (key.upArrow) {
        if (!E.isOnFirstLine(ed)) return setEd(E.up(ed));
        const mv = E.historyPrev(model.hist, ed.value);
        if (mv) {
          model.hist = mv.history;
          setEd(E.recalled(mv.value, 'prev'));
        }
        return;
      }
      if (key.downArrow) {
        if (!E.isOnLastLine(ed)) return setEd(E.down(ed));
        const mv = E.historyNext(model.hist);
        if (mv) {
          model.hist = mv.history;
          setEd(E.recalled(mv.value, 'next'));
        }
        return;
      }
      if (key.leftArrow) return setEd(key.ctrl || key.meta ? E.wordLeft(ed) : E.left(ed));
      if (key.rightArrow) return setEd(key.ctrl || key.meta ? E.wordRight(ed) : E.right(ed));
      if (key.home) return setEd(key.ctrl ? E.bufferStart(ed) : E.lineStart(ed));
      if (key.end) return setEd(key.ctrl ? E.bufferEnd(ed) : E.lineEnd(ed));
      if (key.backspace) return setEd(key.meta ? E.deleteWordBack(ed) : E.backspace(ed));
      if (key.delete) {
        if (isForwardDelete(raw)) return setEd(E.del(ed));
        return setEd(key.meta ? E.deleteWordBack(ed) : E.backspace(ed));
      }

      if (key.ctrl) {
        const ctrlOps: Record<string, (s: E.EditorState) => E.EditorState> = {
          a: E.lineStart,
          e: E.lineEnd,
          b: E.left,
          f: E.right,
          d: E.del,
          h: E.backspace,
          w: E.deleteWordBack,
          u: E.deleteToLineStart,
          k: E.deleteToLineEnd,
        };
        const op = ctrlOps[input];
        if (op) setEd(op(ed));
        return;
      }
      if (key.meta) {
        const metaOps: Record<string, (s: E.EditorState) => E.EditorState> = {
          b: E.wordLeft,
          f: E.wordRight,
          d: E.deleteWordForward,
        };
        const op = metaOps[input];
        if (op) setEd(op(ed));
        return;
      }
      if (key.tab || !input) return;
      // Unrecognised escape sequences (focus events, F-keys...) must not leak in as text; kitty printable keys end in 'u'.
      if (raw.startsWith('\x1b') && !raw.endsWith('u')) return;

      if (input === '?' && !ed.value) {
        model.help = !model.help;
        return rerender();
      }
      if (input.length > 1) return insertText(input, false);
      setEd(E.insert(ed, input));
    },
    [menuFor, setEd, showHint, submit, insertText],
  );

  // Without a TTY (piped stdin) Ink cannot enter raw mode; render read-only instead of throwing.
  useInput(handle, { isActive: !disabled && isRawModeSupported === true });

  // ---------------------------------------------------------------- render
  const menu = menuFor(m, props.commands);
  const sel = Math.min(m.sel, Math.max(0, menu.length - 1));
  const showCursor = !disabled;

  return (
    <Box flexDirection="column" width="100%">
      <Box borderStyle="round" borderColor="gray" borderDimColor paddingX={1} width="100%">
        <Text dimColor={disabled}>{'> '}</Text>
        <Box flexDirection="column" flexGrow={1}>
          {m.ed.value ? (
            renderLines(m.ed, showCursor)
          ) : (
            <Text>
              {showCursor ? <Text inverse>{PLACEHOLDER[0]}</Text> : null}
              <Text dimColor>{showCursor ? PLACEHOLDER.slice(1) : PLACEHOLDER}</Text>
            </Text>
          )}
        </Box>
      </Box>
      {m.help ? (
        <ShortcutsPanel />
      ) : menu.length ? (
        <SlashMenu items={menu} selected={sel} />
      ) : (
        <Footer hint={m.hint} mode={props.permissionMode} empty={!m.ed.value} />
      )}
    </Box>
  );
}

const visible = (t: string) => t.replace(/\t/g, '    ');

function renderLines(ed: E.EditorState, showCursor: boolean) {
  const lines = ed.value.split('\n');
  const { line: curLine } = E.position(ed);
  const curStart = E.lineStartOf(ed.value, ed.cursor);
  return lines.map((line, i) => {
    if (i !== curLine || !showCursor) return <Text key={i}>{visible(line) || ' '}</Text>;
    const col = ed.cursor - curStart;
    const at = E.graphemeAt(ed);
    return (
      <Text key={i}>
        {visible(line.slice(0, col))}
        <Text inverse>{at ? visible(at) : ' '}</Text>
        {visible(line.slice(col + at.length))}
      </Text>
    );
  });
}

function SlashMenu({ items, selected }: { items: SlashCommand[]; selected: number }) {
  const start = Math.max(0, Math.min(selected - MENU_ROWS + 1, items.length - MENU_ROWS));
  const rows = items.slice(start, start + MENU_ROWS);
  const width = Math.max(...items.map((c) => c.name.length + 1 + (c.argumentHint ? c.argumentHint.length + 1 : 0))) + 2;
  return (
    <Box flexDirection="column" paddingX={2}>
      {rows.map((c, i) => {
        const on = start + i === selected;
        const label = `/${c.name}${c.argumentHint ? ` ${c.argumentHint}` : ''}`.padEnd(width);
        return (
          <Text key={c.name} wrap="truncate-end">
            <Text color={on ? 'cyan' : undefined} bold={on}>
              {label}
            </Text>
            <Text color={on ? 'cyan' : undefined} dimColor={!on}>
              {c.description}
            </Text>
          </Text>
        );
      })}
    </Box>
  );
}

function ShortcutsPanel() {
  const width = Math.max(...SHORTCUTS.map(([a]) => a.length)) + 4;
  return (
    <Box flexDirection="column" paddingX={2}>
      {SHORTCUTS.map(([a, b]) => (
        <Text key={a} dimColor>
          {a.padEnd(width)}
          {b}
        </Text>
      ))}
    </Box>
  );
}

function Footer({ hint, mode, empty }: { hint: string | null; mode: PermissionMode; empty: boolean }) {
  const label = MODE_LABEL[mode];
  return (
    <Box paddingX={2}>
      {hint ? (
        <Text dimColor>{hint}</Text>
      ) : label ? (
        <Text color={label.color}>{label.text}</Text>
      ) : (
        <Text dimColor>{empty ? '? for shortcuts' : ' '}</Text>
      )}
    </Box>
  );
}
