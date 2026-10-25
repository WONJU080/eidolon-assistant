export async function api<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error((data as { detail?: string }).detail || `HTTP ${response.status}`);
  }
  return data as T;
}

export function post<T>(base: string, path: string, body?: unknown): Promise<T> {
  return api<T>(base, path, { method: 'POST', body: JSON.stringify(body ?? {}) });
}

export function timeOfDay(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

export function dateTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function duration(ms: number): string {
  if (!ms) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function clip(text: string, limit = 160): string {
  if (!text) return '';
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

export const STATE_LABELS: Record<string, string> = {
  idle: '待机',
  listening: '正在听',
  transcribing: '正在识别',
  thinking: '思考中',
  executing: '正在执行任务',
  waiting_confirmation: '等待确认',
  waiting_answer: '等待回答',
  speaking: '正在说话',
  cancelled: '已取消',
  error: '出错',
};

export const STATE_TONE: Record<string, string> = {
  idle: 'idle',
  listening: 'listen',
  transcribing: 'blue',
  thinking: 'violet',
  executing: 'gold',
  waiting_confirmation: 'amber',
  waiting_answer: 'amber',
  speaking: 'teal',
  cancelled: 'danger',
  error: 'danger',
};

export function sourceLabel(source?: string): string {
  if (source === 'voice') return '语音';
  if (source === 'direct') return '直接命令';
  return '文字';
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}
