// Shared contract between core (src/core), input UI (src/ui/input) and render UI (src/ui/render).
// Keep this file the single source of truth; change it only in coordination.

export type ToolStatus = 'running' | 'done' | 'error' | 'denied';

export type Entry =
  | { kind: 'user'; id: string; text: string }
  /** Assistant text. `streaming` is true while deltas are still arriving. */
  | { kind: 'assistant'; id: string; text: string; streaming: boolean; parentToolUseId: string | null }
  | { kind: 'thinking'; id: string; text: string; streaming: boolean }
  /** One tool call. `id` is the tool_use_id; `result` is the tool_result text once it arrives. */
  | {
      kind: 'tool';
      id: string;
      name: string;
      input: Record<string, unknown>;
      status: ToolStatus;
      result?: string;
      parentToolUseId: string | null;
    }
  | { kind: 'system'; id: string; text: string; level: 'info' | 'warn' | 'error' };

export type PermissionMode = 'default' | 'acceptEdits' | 'plan' | 'bypassPermissions';

export type PermissionDecision = 'allow' | 'allow_always' | 'deny';

export interface PermissionRequest {
  id: string;
  toolName: string;
  input: Record<string, unknown>;
  /** Human-readable reason from the CLI, if any. */
  reason?: string;
  /** Whether "allow always (this session)" can be offered. */
  canAllowAlways: boolean;
}

export interface SlashCommand {
  name: string; // without the leading "/"
  description: string;
  argumentHint?: string;
  /** 'local' commands are handled by cfharness itself; 'remote' are sent to the CLI as a prompt. */
  source: 'local' | 'remote';
}

export interface SessionStatus {
  /** True from send() until the turn's `result` message (or an interrupt) arrives. */
  busy: boolean;
  /** Short label for the spinner while busy, e.g. "Thinking", "Running Bash". */
  activity?: string;
  /** ms epoch when the current turn started, for the elapsed timer. */
  turnStartedAt?: number;
  model?: string;
  sessionId?: string;
  cwd: string;
  permissionMode: PermissionMode;
  totalCostUsd: number;
  inputTokens: number;
  outputTokens: number;
  /** Output tokens streamed so far in the current turn (for the spinner). */
  turnOutputTokens: number;
}

export interface SessionState {
  entries: Entry[];
  status: SessionStatus;
  /** At most one permission prompt is shown at a time; others queue inside the session. */
  pendingPermission: PermissionRequest | null;
  slashCommands: SlashCommand[];
}

export interface SessionOptions {
  cwd: string;
  model?: string;
  resume?: string;
  permissionMode?: PermissionMode;
  /** Allow switching into bypassPermissions (only when launched with --dangerously-skip-permissions). */
  allowBypass?: boolean;
}

/** Implemented by src/core/session.ts (class HarnessSession). UI only talks to this interface. */
export interface Session {
  getState(): SessionState;
  /** Returns an unsubscribe function. Listener is called after every state change. Compatible with useSyncExternalStore. */
  subscribe(listener: () => void): () => void;
  start(): void;
  /** Send a user prompt. Allowed while busy (it queues inside the CLI). */
  send(text: string): void;
  interrupt(): Promise<void>;
  answerPermission(id: string, decision: PermissionDecision): void;
  setPermissionMode(mode: PermissionMode): Promise<void>;
  setModel(model?: string): Promise<void>;
  /** Append a local system line (used by local slash commands). */
  notify(text: string, level?: 'info' | 'warn' | 'error'): void;
  /** Clear the visible transcript (does not reset the CLI session). */
  clearTranscript(): void;
  close(): Promise<void>;
}
