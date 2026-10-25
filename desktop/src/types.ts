export type HarnessState = {
  status: 'stopped' | 'starting' | 'ready' | 'busy' | 'error' | 'killed';
  session: string | null;
  pid: number | null;
  permission_mode: 'read-only' | 'danger-full-access';
  pending_permissions: PermissionRequest[];
  last_error: string;
  stderr_tail: string[];
  active: boolean;
};

export type PermissionOption = { optionId: string; name: string; kind: string };

export type PermissionRequest = {
  id: string;
  tool_call_id: string;
  title: string;
  command: string;
  justification: string;
  sandbox_permissions: string;
  kind: string;
  options: PermissionOption[];
  requested_at: number;
};

export type AssistantState = {
  state: string;
  detail: string;
  character?: string;
  voice_ready: boolean;
  wake_ready: boolean;
  last_error: string;
  current_task_id: string | null;
  current_task: string;
  harness: HarnessState;
  pending: { id: string; kind: 'task' | 'permission' | 'confirm'; ja: string; zh: string; task?: string } | null;
  audio: { input_devices: Array<{ index: number; name: string; default: boolean }>; level: number };
};

export type CharacterConfig = {
  id: string;
  name: string;
  name_zh: string;
  color: string;
  wake_words: string[];
  wake_reply_zh: string;
};

export type Config = {
  app_name: string;
  character: CharacterConfig;
  harness: { command: string; profile: string; permission_mode: string; auto_approve: boolean };
  workspace: string;
  services: {
    llm: { model: string; ready: boolean };
    stt: { model: string; ready: boolean };
    tts: { model: string; ready: boolean };
    wake: { words: string[]; enabled: boolean };
  };
  limits: { max_prompt_chars: number; permission_timeout_s: number };
};

export type CharacterMeta = {
  id: string;
  name: string;
  name_zh: string;
  name_romaji: string;
  description: string;
  color: string;
  voice_id: string;
  wake_words: string[];
  wake_reply_tts: string;
  wake_reply_zh: string;
  tts_language: string;
  avatar: boolean;
};

export type CharacterDetail = CharacterMeta & { sheet_text: string };

export type AudioDevice = { index: number; name: string; default: boolean };

export type EditableSettings = {
  audio: { input_device: string; devices: AudioDevice[] };
  recording: { vad_threshold: number; silence_ms: number; max_utterance_s: number };
  wake: { enabled: boolean; threshold: number; score: number; threads: number };
  voice_ready: boolean;
  wake_ready: boolean;
};

export type TaskSummary = {
  id: string;
  created_at: number;
  updated_at: number;
  source: 'voice' | 'text' | 'direct';
  raw_input: string;
  normalized: string;
  constraints: string[];
  requires_confirmation: number;
  status: string;
  stop_reason: string;
  summary_ja: string;
  summary_zh: string;
  error: string;
  harness_session: string;
  duration_ms: number;
  permission_denials: number;
};

export type TaskEvent = { seq: number; ts: number; kind: string; payload: unknown };

export type Message = {
  key: string;
  role: 'user' | 'assistant' | 'system';
  text?: string;
  ja?: string;
  zh?: string;
  level?: 'info' | 'warn' | 'error';
  source?: string;
  tag?: string;
  speak?: boolean;
  at: number;
};

export type RawEvent = {
  key: string;
  at: number;
  lane: 'acp' | 'tool' | 'lifecycle';
  title: string;
  detail: string;
};

export type Activity = { key: string; at: number; label: string; tone?: 'plain' | 'gold' | 'ok' | 'warn' };

export type AppInfo = {
  apiBase: string;
  version: string;
  projectRoot: string;
  dev: boolean;
  backendLog: string[];
  alwaysOnTop: boolean;
};

export type EidolonBridge = {
  info: () => Promise<AppInfo>;
  setAlwaysOnTop: (value: boolean) => Promise<boolean>;
  hide: () => Promise<void>;
  quit: () => Promise<void>;
  openPath: (target: string) => Promise<string>;
  openExternal: (target: string) => Promise<void>;
  onAction: (callback: (action: string) => void) => () => void;
  onBackendLog: (callback: (line: string) => void) => () => void;
  onAlwaysOnTop: (callback: (value: boolean) => void) => () => void;
};

declare global {
  interface Window {
    eidolon?: EidolonBridge;
  }
}
