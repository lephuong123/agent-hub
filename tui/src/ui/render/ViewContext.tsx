import { createContext, useContext, useEffect, useState } from 'react';

/** How transcript items react to the user. Stable for the life of a view, so memoized items do not re-render. */
export interface ViewActions {
  /** False in the inline (scrollback) UI: nothing is clickable or expandable after it is printed. */
  interactive: boolean;
  toggle(id: string): void;
  /** Open the detail view of an Agent/Task tool (or any task's tool call). */
  open(toolUseId: string): void;
}

export const ViewContext = createContext<ViewActions>({ interactive: false, toggle: () => {}, open: () => {} });

export const useViewActions = () => useContext(ViewContext);

/** Re-render every `ms` while `active` (for live elapsed timers). */
export function useTicker(active: boolean, ms = 1000): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setTick((n) => n + 1), ms);
    return () => clearInterval(t);
  }, [active, ms]);
}
