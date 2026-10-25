import { useCallback, useEffect, useRef, useState } from 'react';
import { TitleBar } from './components/TitleBar';
import { StatusRail } from './components/StatusRail';
import { Transcript } from './components/Transcript';
import { Composer } from './components/Composer';
import { PendingCard } from './components/PendingCard';
import { RawDrawer } from './components/RawDrawer';
import { SettingsSheet } from './components/SettingsSheet';
import type {
  Activity,
  AppInfo,
  AssistantState,
  Config,
  CharacterMeta,
  EditableSettings,
  Message,
  PermissionRequest,
  RawEvent,
  TaskSummary,
} from './types';
import { clip, post } from './lib';

type PendingPayload =
  | { id: string; kind: 'task'; ja: string; zh: string; task: string }
  | { id: string; kind: 'permission'; ja: string; zh: string; request: PermissionRequest };

const EMPTY_PERMISSION: PermissionRequest = {
  id: '',
  tool_call_id: '',
  title: '',
  command: '',
  justification: '',
  sandbox_permissions: '',
  kind: '',
  options: [],
  requested_at: 0,
};

export default function App() {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [state, setState] = useState<AssistantState | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [rawEvents, setRawEvents] = useState<RawEvent[]>([]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [characters, setCharacters] = useState<CharacterMeta[]>([]);
  const [editable, setEditable] = useState<EditableSettings | null>(null);
  const [pending, setPending] = useState<PendingPayload | null>(null);
  const [rawOpen, setRawOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [alwaysOnTop, setAlwaysOnTop] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [backendLog, setBackendLog] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);

  const socket = useRef<WebSocket | null>(null);
  const counter = useRef(0);
  const permissionById = useRef<Record<string, PermissionRequest>>({});
  const toastTimer = useRef<number | null>(null);
  const apiBase = info?.apiBase ?? '';

  const nextKey = useCallback(() => {
    counter.current += 1;
    return `k${counter.current}`;
  }, []);

  const showToast = useCallback(
    (text: string) => {
      setToast(text);
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
      toastTimer.current = window.setTimeout(() => setToast(null), 3200);
    },
    [],
  );

  const refreshState = useCallback(async () => {
    if (!apiBase) return;
    try {
      const next = (await fetch(`${apiBase}/api/state`).then((r) => r.json())) as AssistantState;
      setState(next);
    } catch {
      /* ignore */
    }
  }, [apiBase]);

  const refreshTasks = useCallback(async () => {
    if (!apiBase) return;
    try {
      const data = (await fetch(`${apiBase}/api/tasks?limit=80`).then((r) => r.json())) as {
        tasks: TaskSummary[];
      };
      setTasks(data.tasks ?? []);
    } catch {
      /* ignore */
    }
  }, [apiBase]);

  const refreshCharacters = useCallback(async () => {
    if (!apiBase) return;
    try {
      const data = (await fetch(`${apiBase}/api/characters`).then((r) => r.json())) as {
        characters: CharacterMeta[];
        active: string;
      };
      setCharacters(data.characters ?? []);
    } catch {
      /* ignore */
    }
  }, [apiBase]);

  const refreshSettings = useCallback(async () => {
    if (!apiBase) return;
    try {
      const data = (await fetch(`${apiBase}/api/settings`).then((r) => r.json())) as EditableSettings;
      setEditable(data);
    } catch {
      /* ignore */
    }
  }, [apiBase]);

  const refreshConfig = useCallback(async () => {
    if (!apiBase) return;
    try {
      const data = (await fetch(`${apiBase}/api/config`).then((r) => r.json())) as {
        config: Config;
        state: AssistantState;
      };
      setConfig(data.config);
      setState(data.state);
    } catch {
      /* ignore */
    }
  }, [apiBase]);

  const pushRaw = useCallback(
    (lane: RawEvent['lane'], title: string, detail: string, at = Date.now() / 1000) => {
      setRawEvents((previous) => {
        const next = [
          ...previous,
          { key: `${at}-${Math.random().toString(36).slice(2, 8)}`, at, lane, title, detail },
        ];
        return next.length > 400 ? next.slice(next.length - 400) : next;
      });
    },
    [],
  );

  const addActivity = useCallback((label: string, tone: Activity['tone'] = 'plain') => {
    setActivity((previous) => [
      ...previous.slice(-7),
      { key: `a${Date.now()}-${Math.random().toString(36).slice(2, 6)}`, at: Date.now() / 1000, label, tone },
    ]);
  }, []);

  // ------------------------------------------------------------- bootstrapping
  useEffect(() => {
    void (async () => {
      const value = await window.eidolon?.info();
      if (value) {
        setInfo(value);
        setAlwaysOnTop(value.alwaysOnTop);
        setBackendLog(value.backendLog);
      } else {
        // opened in a plain browser against the backend's own origin
        setInfo({
          apiBase: window.location.origin,
          version: 'dev',
          projectRoot: '',
          dev: true,
          backendLog: [],
          alwaysOnTop: false,
        });
      }
    })();
  }, []);

  useEffect(() => {
    if (!apiBase) return;
    void refreshConfig();
    void refreshTasks();
    void refreshCharacters();
    const timer = window.setInterval(() => void refreshState(), 2500);
    return () => window.clearInterval(timer);
  }, [apiBase, refreshConfig, refreshTasks, refreshState, refreshCharacters]);

  useEffect(() => {
    if (!settingsOpen) return;
    void refreshCharacters();
    void refreshSettings();
  }, [settingsOpen, refreshCharacters, refreshSettings]);

  // ------------------------------------------------------------------- socket
  useEffect(() => {
    if (!apiBase) return;
    let cancelled = false;
    let retry: number | null = null;

    const connect = () => {
      if (cancelled) return;
      const url = `${apiBase.replace('http', 'ws')}/ws`;
      const ws = new WebSocket(url);
      socket.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) retry = window.setTimeout(connect, 1200);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (event) => {
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(event.data as string) as Record<string, unknown>;
        } catch {
          return;
        }
        handleEvent(payload);
      };
    };

    const handleEvent = (event: Record<string, unknown>) => {
      const type = event.type as string;
      if (type === 'hello') {
        setConfig(event.config as Config);
        setState(event.state as AssistantState);
        return;
      }
      if (type === 'message') {
        const role = event.role as Message['role'];
        setMessages((previous) => [
          ...previous,
          {
            key: nextKey(),
            role,
            text: event.text as string | undefined,
            ja: event.ja as string | undefined,
            zh: event.zh as string | undefined,
            level: event.level as Message['level'],
            source: event.source as string | undefined,
            tag: event.tag as string | undefined,
            speak: event.speak as boolean | undefined,
            at: (event.at as number) ?? Date.now() / 1000,
          },
        ]);
        return;
      }
      if (type === 'state') {
        const nextState = event.state as string;
        const detail = (event.detail as string) ?? '';
        setState((previous) => (previous ? { ...previous, state: nextState, detail } : previous));
        if (nextState === 'idle') window.setTimeout(() => void refreshState(), 300);
        return;
      }
      if (type === 'intent') {
        addActivity(`意图 · ${String(event.intent)}`, event.intent === 'task' ? 'gold' : 'plain');
        return;
      }
      if (type === 'normalized') {
        addActivity(`标准化 · ${clip(String(event.task), 42)}`, 'gold');
        return;
      }
      if (type === 'task_created') {
        addActivity(`任务 ${clip(String(event.normalized), 36)}`, 'gold');
        void refreshTasks();
        return;
      }
      if (type === 'task_finished') {
        void refreshTasks();
        void refreshState();
        return;
      }
      if (type === 'confirmation_request') {
        setPending((previous) => {
          if (event.kind === 'permission') {
            const request =
              permissionById.current[String(event.id)] ??
              ({
                ...EMPTY_PERMISSION,
                id: String(event.id),
                command: '',
              } as PermissionRequest);
            return { id: String(event.id), kind: 'permission', ja: String(event.ja), zh: String(event.zh), request };
          }
          return {
            id: String(event.id),
            kind: 'task',
            ja: String(event.ja),
            zh: String(event.zh),
            task: String(event.task ?? ''),
          };
        });
        return;
      }
      if (type === 'confirmation_closed') {
        setPending((previous) => (previous?.id === event.id ? null : previous));
        void refreshState();
        return;
      }
      if (type === 'permission_request') {
        const request = event.request as PermissionRequest;
        permissionById.current[request.id] = request;
        pushRaw('lifecycle', 'permission_request', JSON.stringify(request, null, 2));
        return;
      }
      if (type === 'tool') {
        const tool = event.tool as { title?: string; status?: string; command?: string };
        pushRaw('tool', `${tool.title ?? 'tool'} · ${tool.status ?? ''}`, JSON.stringify(event.tool, null, 2),
          (event.at as number) ?? Date.now() / 1000);
        return;
      }
      if (type === 'acp_event') {
        pushRaw('acp', String(event.method), JSON.stringify(event.params, null, 2),
          (event.at as number) ?? Date.now() / 1000);
        return;
      }
      if (type === 'harness') {
        const status = String(event.status);
        setState((previous) =>
          previous ? { ...previous, harness: { ...previous.harness, status: status as AssistantState['harness']['status'] } } : previous,
        );
        if (status === 'error') showToast(`Harness 出错：${String(event.error ?? '')}`);
        return;
      }
      if (type === 'character') {
        void refreshConfig();
        void refreshCharacters();
        return;
      }
      if (type === 'wake') {
        addActivity(`唤醒 · ${String(event.word)}`, 'gold');
        return;
      }
      if (type === 'harness_pollution') {
        pushRaw('lifecycle', 'stdout 非协议输出', String(event.raw));
      }
    };

    connect();
    return () => {
      cancelled = true;
      if (retry) window.clearTimeout(retry);
      socket.current?.close();
    };
  }, [apiBase, addActivity, nextKey, pushRaw, refreshState, refreshTasks, refreshCharacters, showToast]);

  // ------------------------------------------------------------------ actions
  const send = useCallback(
    async (text: string) => {
      try {
        const result = await post<{ ok: boolean; reason?: string }>(apiBase, '/api/text', { text });
        if (!result.ok) showToast(result.reason === 'busy' ? '正在忙，等这次结束' : '发送失败');
      } catch (error) {
        showToast(`发送失败：${String(error)}`);
      }
    },
    [apiBase, showToast],
  );

  const listen = useCallback(async () => {
    try {
      const result = await post<{ ok: boolean; reason?: string }>(apiBase, '/api/listen', { action: 'start' });
      if (!result.ok) showToast(result.reason === 'no_audio' ? '麦克风不可用' : '现在不能听');
    } catch (error) {
      showToast(`失败：${String(error)}`);
    }
  }, [apiBase, showToast]);

  const pttStart = useCallback(async () => {
    try {
      const result = await post<{ ok: boolean; reason?: string }>(apiBase, '/api/ptt', { action: 'start' });
      if (!result.ok) showToast(result.reason === 'busy' ? '正在忙' : '麦克风不可用');
    } catch {
      /* ignore */
    }
  }, [apiBase, showToast]);

  const pttStop = useCallback(async () => {
    try {
      await post(apiBase, '/api/ptt', { action: 'stop' });
    } catch {
      /* ignore */
    }
  }, [apiBase]);

  const cancel = useCallback(async () => {
    try {
      await post(apiBase, '/api/cancel', {});
    } catch (error) {
      showToast(`中断失败：${String(error)}`);
    }
  }, [apiBase, showToast]);

  const newSession = useCallback(async () => {
    try {
      const data = await post<{ ok: boolean; session?: string; error?: string }>(apiBase, '/api/session/new', {});
      showToast(data.ok ? `新会话 ${data.session?.slice(0, 8) ?? ''}` : `失败：${data.error ?? ''}`);
    } catch (error) {
      showToast(`失败：${String(error)}`);
    }
  }, [apiBase, showToast]);

  const answer = useCallback(
    async (allow: boolean) => {
      if (!pending) return;
      try {
        await post(apiBase, '/api/confirm', { id: pending.id, allow });
        setPending(null);
        addActivity(allow ? '已允许' : '已拒绝', allow ? 'ok' : 'warn');
      } catch (error) {
        showToast(`回答失败：${String(error)}`);
      }
    },
    [apiBase, pending, addActivity, showToast],
  );

  const togglePin = useCallback(
    async (value: boolean) => {
      const applied = (await window.eidolon?.setAlwaysOnTop(value)) ?? value;
      setAlwaysOnTop(applied);
    },
    [],
  );

  useEffect(() => {
    if (!window.eidolon) return;
    const offAction = window.eidolon.onAction((action) => {
      if (action === 'listen') void listen();
      if (action === 'cancel') void cancel();
      if (action === 'settings') setSettingsOpen(true);
      if (action === 'new-session') void newSession();
    });
    const offLog = window.eidolon.onBackendLog((line) => {
      setBackendLog((previous) => [...previous.slice(-400), line]);
    });
    const offPin = window.eidolon.onAlwaysOnTop((value) => setAlwaysOnTop(value));
    return () => {
      offAction();
      offLog();
      offPin();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);


  // ------------------------------------------------------------- derived state
  const busyStates = new Set(['listening', 'transcribing', 'thinking', 'executing', 'waiting_confirmation', 'waiting_answer', 'speaking']);
  const busy = Boolean(state && (busyStates.has(state.state) || state.harness.active));
  const allowText = Boolean(
    state && ['waiting_answer', 'waiting_confirmation'].includes(state.state),
  ) || !busy;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        void cancel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [cancel]);

  return (
    <div className="app">
      <TitleBar
        character={config?.character ?? null}
        state={state}
        alwaysOnTop={alwaysOnTop}
        onTogglePin={() => void togglePin(!alwaysOnTop)}
        onSettings={() => setSettingsOpen(true)}
        onRaw={() => setRawOpen(true)}
        onHide={() => void window.eidolon?.hide()}
      />
      <StatusRail state={state} config={config} />
      {!connected ? <div className="offline-banner">正在连接后端…</div> : null}
      <Transcript
        messages={messages}
        activity={activity}
        characterName={config?.character.name_zh ?? '拉姆蕾萨尔'}
        wakeWords={config?.character.wake_words ?? []}
        onExample={(text) => void send(text)}
        scrollSignal={pending?.id}
      />
      {pending ? (
        <PendingCard
          pending={pending}
          timeoutSeconds={config?.limits.permission_timeout_s ?? 120}
          onAnswer={(allow) => void answer(allow)}
        />
      ) : null}
      <Composer
        state={state}
        busy={busy}
        onSend={(text) => void send(text)}
        onListen={() => void listen()}
        onPttStart={() => void pttStart()}
        onPttStop={() => void pttStop()}
        onCancel={() => void cancel()}
        ttsSpeaking={state?.state === 'speaking'}
        allowText={allowText}
      />
      <RawDrawer
        open={rawOpen}
        onClose={() => setRawOpen(false)}
        apiBase={apiBase}
        events={rawEvents}
        currentTaskId={state?.current_task_id ?? null}
        onRefreshTasks={() => void refreshTasks()}
        tasks={tasks}
        onToast={showToast}
      />
      <SettingsSheet
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        apiBase={apiBase}
        config={config}
        state={state}
        alwaysOnTop={alwaysOnTop}
        onTogglePin={(value) => void togglePin(value)}
        onRefresh={() => {
          void refreshConfig();
          void refreshState();
        }}
        onToast={showToast}
        backendLog={backendLog}
        characters={characters}
        activeId={config?.character.id ?? state?.character ?? ''}
        editable={editable}
        onRefreshCharacters={refreshCharacters}
        onRefreshSettings={refreshSettings}
      />
      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  );
}
