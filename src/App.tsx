import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Box, Text, useInput, useStdin, useWindowSize } from 'ink';
import type { Session, TaskInfo } from './types.js';
import { useHarness } from './useHarness.js';
import { PromptInput } from './ui/input/PromptInput.js';
import { MessageView, PermissionDialog, Spinner, StatusBar, Welcome, planTranscript } from './ui/render/index.js';
import { ViewContext, useTicker, type ViewActions } from './ui/render/ViewContext.js';
import { Clickable, MouseProvider, type DragHandlers, type MouseSource } from './ui/term/MouseContext.js';
import type { Selection } from './ui/term/selection.js';
import { ScrollView, WHEEL_ROWS, type ScrollApi } from './ui/term/ScrollView.js';
import { AgentView } from './ui/fullscreen/AgentView.js';
import { LINGER_MS, TaskPanel, visibleTasks } from './ui/fullscreen/TaskPanel.js';

const SHORTCUTS: [string, string][] = [
  ['wheel / PgUp / PgDn to scroll', 'click a tool to expand it'],
  ['ctrl+o to expand everything', 'click an agent to open it'],
  ['↓ from the prompt for tasks', 'ctrl+b to background a task'],
  ['drag to select and copy', 'shift+drag for native selection'],
];

export interface AppProps {
  session: Session;
  allowBypass?: boolean;
  /** Mouse events, or null when mouse capture is off (--no-mouse). */
  mouse: MouseSource | null;
  /** Drag-to-select overlay; null when it cannot run (no mouse, or Ink internals not found). */
  selection?: Selection | null;
  /** Put copied text on the clipboard. */
  onCopy?(text: string): Promise<void>;
}

/** Fullscreen UI: the transcript lives in a scrollable, clickable viewport on the alternate screen. */
export function App({ session, allowBypass = false, mouse, selection = null, onCopy }: AppProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const onClear = useCallback(() => setExpanded(new Set()), []);
  const { state, quit, submit, cycleMode, background } = useHarness(session, allowBypass, onClear);
  const { status, pendingPermission, entries, tasks } = state;
  const { columns, rows } = useWindowSize();
  const { isRawModeSupported } = useStdin();

  const [all, setAll] = useState(false);
  const [view, setView] = useState<string | null>(null);
  const [focus, setFocus] = useState<'prompt' | 'tasks'>('prompt');
  const [selected, setSelected] = useState(0);
  const mainScroll = useRef<ScrollApi>(null);
  const detailScroll = useRef<ScrollApi>(null);
  const active = () => (view ? detailScroll : mainScroll).current;

  const [toast, setToast] = useState<string | null>(null);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(t);
  }, [toast]);
  const copyRef = useRef(onCopy);
  copyRef.current = onCopy;
  const drag = useMemo<DragHandlers | null>(
    () =>
      selection && {
        drag: (a, b) => selection.set(a, b),
        end: (a, b) => {
          selection.set(a, b);
          const text = selection.text();
          if (!text) return selection.clear();
          copyRef.current?.(text).then(
            () => setToast(`Copied ${text.length} ${text.length === 1 ? 'character' : 'characters'} to the clipboard`),
            () => setToast('Could not copy to the clipboard'),
          );
        },
        clear: () => selection.clear(),
      },
    [selection],
  );

  const tasksByTool = useMemo(() => new Map(tasks.filter((t) => t.toolUseId).map((t) => [t.toolUseId!, t])), [tasks]);
  // Re-render while finished tasks linger in the panel, so they drop out on time.
  useTicker(tasks.some((t) => t.endedAt != null && Date.now() - t.endedAt < LINGER_MS));
  const panelTasks = visibleTasks(tasks);
  const sel = Math.min(selected, Math.max(0, panelTasks.length - 1));
  const tasksFocused = focus === 'tasks' && panelTasks.length > 0;
  const viewTask = view ? tasksByTool.get(view) : undefined;

  const actions = useMemo<ViewActions>(
    () => ({
      interactive: true,
      toggle: (id) =>
        setExpanded((s) => {
          const next = new Set(s);
          if (!next.delete(id)) next.add(id);
          return next;
        }),
      open: (id) => setView(id),
    }),
    [],
  );

  const openTask = (t: TaskInfo) => {
    if (t.toolUseId) setView(t.toolUseId);
    else session.notify(`Task "${t.description}" has no transcript to show.`, 'warn');
  };
  const stopTask = (t?: TaskInfo) => {
    if (t?.status !== 'running') return;
    session.stopTask(t.id).catch((e: unknown) => session.notify(`Could not stop task: ${e instanceof Error ? e.message : String(e)}`, 'error'));
  };

  // A task view whose entries were cleared has nothing to show.
  useEffect(() => {
    if (view && !viewTask && !entries.some((e) => e.id === view)) setView(null);
  }, [view, viewTask, entries]);

  useInput(
    (input, key) => {
      selection?.clear();
      if (key.pageUp) return active()?.page(-1);
      if (key.pageDown) return active()?.page(1);
      if (key.ctrl && input === 'o') return setAll((a) => !a);
      if (pendingPermission) return; // the dialog owns every other key
      if (view) {
        if (key.escape || (key.ctrl && input === 'c') || key.leftArrow) return setView(null);
        if (key.upArrow) return active()?.scrollBy(-1);
        if (key.downArrow) return active()?.scrollBy(1);
        if (key.home) return active()?.toTop();
        if (key.end) return active()?.toBottom();
        if (input === 'x') return stopTask(viewTask);
        if (key.ctrl && input === 'b') return background();
        return;
      }
      if (tasksFocused) {
        if (key.escape || (key.ctrl && input === 'c')) return setFocus('prompt');
        if (key.upArrow) return sel === 0 ? setFocus('prompt') : setSelected(sel - 1);
        if (key.downArrow) return setSelected(Math.min(sel + 1, panelTasks.length - 1));
        if (key.return || key.rightArrow) return openTask(panelTasks[sel]!);
        if (input === 'x') return stopTask(panelTasks[sel]);
        if (key.ctrl && input === 'b') return background();
      }
    },
    { isActive: isRawModeSupported === true },
  );

  const onSubmit = useCallback(
    (text: string) => {
      mainScroll.current?.toBottom();
      submit(text);
    },
    [submit],
  );

  const { items } = useMemo(() => planTranscript(entries), [entries]);

  return (
    <ViewContext.Provider value={actions}>
      <MouseProvider source={mouse} drag={drag} onUnhandledWheel={(e) => active()?.scrollBy(e.action === 'wheelUp' ? -WHEEL_ROWS : WHEEL_ROWS)}>
        <Box flexDirection="column" width={columns} height={rows}>
          {view ? (
            <AgentView
              entries={entries}
              toolUseId={view}
              task={viewTask}
              tasksByTool={tasksByTool}
              expanded={expanded}
              all={all}
              scrollRef={detailScroll}
              onBack={() => setView(null)}
              onStop={viewTask ? () => stopTask(viewTask) : undefined}
            />
          ) : (
            <ScrollView apiRef={mainScroll}>
              <Welcome cwd={status.cwd} model={status.model} />
              {items.map((item) => (
                <MessageView
                  key={item.entry.id}
                  entry={item.entry}
                  childEntries={item.children}
                  task={tasksByTool.get(item.entry.id)}
                  expanded={all || expanded.has(item.entry.id)}
                />
              ))}
            </ScrollView>
          )}
          <Box flexDirection="column" flexShrink={0}>
            {status.busy && !pendingPermission && (
              <Spinner
                activity={status.activity ?? 'Thinking'}
                startedAt={status.turnStartedAt}
                tokens={status.turnOutputTokens}
                hint={tasks.some((t) => t.status === 'running' && !t.backgrounded) ? 'ctrl+b to run in background' : undefined}
              />
            )}
            {pendingPermission && (
              <PermissionDialog
                key={pendingPermission.id}
                request={pendingPermission}
                onDecision={(d) => session.answerPermission(pendingPermission.id, d)}
              />
            )}
            {/* Hidden rather than unmounted in the task view, so the draft and history survive. */}
            <Box display={view ? 'none' : 'flex'} flexDirection="column">
              <Clickable flexDirection="column" onClick={() => setFocus('prompt')}>
                <PromptInput
                  onSubmit={onSubmit}
                  commands={state.slashCommands}
                  disabled={!!pendingPermission || tasksFocused || !!view}
                  busy={status.busy}
                  permissionMode={status.permissionMode}
                  onInterrupt={() => void session.interrupt()}
                  onCyclePermissionMode={cycleMode}
                  onBackground={background}
                  onDownAtEnd={() => panelTasks.length > 0 && setFocus('tasks')}
                  onExit={quit}
                  extraShortcuts={SHORTCUTS}
                />
              </Clickable>
            </Box>
            {!view && <TaskPanel tasks={panelTasks} focused={tasksFocused} selected={sel} onOpen={openTask} />}
            {toast ? <Text color="green">{`✓ ${toast}`}</Text> : <StatusBar status={status} />}
          </Box>
        </Box>
      </MouseProvider>
    </ViewContext.Provider>
  );
}
