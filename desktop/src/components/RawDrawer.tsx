import { useCallback, useEffect, useState } from 'react';
import type { RawEvent, TaskEvent, TaskSummary } from '../types';
import { clip, dateTime, duration, post, shortId } from '../lib';

type Props = {
  open: boolean;
  onClose: () => void;
  apiBase: string;
  events: RawEvent[];
  currentTaskId: string | null;
  onRefreshTasks: () => void;
  tasks: TaskSummary[];
  onToast: (text: string) => void;
};

type Tab = 'tasks' | 'events' | 'direct';

export function RawDrawer({ open, onClose, apiBase, events, currentTaskId, onRefreshTasks, tasks, onToast }: Props) {
  const [tab, setTab] = useState<Tab>('tasks');
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskSummary | null>(null);
  const [taskEvents, setTaskEvents] = useState<TaskEvent[]>([]);
  const [direct, setDirect] = useState('');
  const [sending, setSending] = useState(false);

  useEffect(() => {
    if (open) onRefreshTasks();
  }, [open, onRefreshTasks]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setTaskEvents([]);
      return;
    }
    void (async () => {
      const [taskResponse, eventResponse] = await Promise.all([
        fetch(`${apiBase}/api/tasks/${selected}`).then((r) => r.json()),
        fetch(`${apiBase}/api/tasks/${selected}/events`).then((r) => r.json()),
      ]);
      setDetail(taskResponse.task ?? null);
      setTaskEvents(eventResponse.events ?? []);
    })();
  }, [selected, apiBase]);

  const sendDirect = useCallback(async () => {
    const value = direct.trim();
    if (!value) return;
    setSending(true);
    try {
      const result = await post<{ ok: boolean; reason?: string }>(apiBase, '/api/direct', { text: value });
      if (result.ok) {
        setDirect('');
        onToast('直接命令已发送给 Harness');
      } else {
        onToast(`没有发送：${result.reason ?? '未知原因'}`);
      }
    } catch (error) {
      onToast(`发送失败：${String(error)}`);
    } finally {
      setSending(false);
    }
  }, [apiBase, direct, onToast]);

  if (!open) return null;

  return (
    <aside className="drawer">
      <div className="drawer-head">
        <div>
          <div className="drawer-title">原始访问</div>
          <div className="drawer-sub">绕过角色的地方，所有操作都有记录</div>
        </div>
        <button className="icon-button" onClick={onClose} title="关闭">
          ✕
        </button>
      </div>
      <div className="tabs">
        <button className={tab === 'tasks' ? 'active' : ''} onClick={() => setTab('tasks')}>
          任务详情
        </button>
        <button className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')}>
          实时事件
        </button>
        <button className={tab === 'direct' ? 'active' : ''} onClick={() => setTab('direct')}>
          直接命令
        </button>
      </div>

      {tab === 'tasks' ? (
        <div className="drawer-body">
          {detail ? (
            <div className="task-detail">
              <button className="link" onClick={() => setSelected(null)}>← 回到任务列表</button>
              <div className="detail-block">
                <div className="mono-label">原始输入</div>
                <pre>{detail.raw_input}</pre>
              </div>
              <div className="detail-block">
                <div className="mono-label">标准化命令</div>
                <pre>{detail.normalized || '（直接命令，未标准化）'}</pre>
              </div>
              {detail.constraints.length > 0 ? (
                <div className="detail-block">
                  <div className="mono-label">约束</div>
                  <ul>
                    {detail.constraints.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              <div className="detail-block">
                <div className="mono-label">结果</div>
                <div className="detail-result">
                  <span className={`task-status status-${detail.status}`}>{detail.status}</span>
                  <span>{detail.stop_reason}</span>
                  {detail.summary_ja ? <span className="ja-inline">{detail.summary_ja}</span> : null}
                  {detail.summary_zh ? <span>{detail.summary_zh}</span> : null}
                  {detail.error ? <span className="warn">{detail.error}</span> : null}
                </div>
              </div>
              <div className="detail-block">
                <div className="mono-label">ACP 事件（{taskEvents.length}）</div>
                <div className="event-list">
                  {taskEvents.map((event) => (
                    <details key={event.seq} className="event-row">
                      <summary>
                        <span className="event-seq">#{event.seq}</span>
                        <span className="event-kind">{event.kind}</span>
                        <span className="event-time">{dateTime(event.ts)}</span>
                      </summary>
                      <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                    </details>
                  ))}
                  {taskEvents.length === 0 ? <p className="drawer-empty">没有事件。</p> : null}
                </div>
              </div>
            </div>
          ) : null}
          <div className="task-list">
            {tasks.length === 0 ? <p className="drawer-empty">还没有任务记录。</p> : null}
            {tasks.map((task) => (
              <button
                key={task.id}
                className={`task-row ${selected === task.id ? 'selected' : ''} ${task.id === currentTaskId ? 'running' : ''}`}
                onClick={() => setSelected(task.id)}
              >
                <div className="task-row-top">
                  <span className={`task-source source-${task.source}`}>{task.source}</span>
                  <span className="task-time">{dateTime(task.created_at)}</span>
                  <span className={`task-status status-${task.status}`}>{task.status}</span>
                </div>
                <div className="task-row-title">{clip(task.normalized || task.raw_input, 90)}</div>
                <div className="task-row-meta">
                  <span>{shortId(task.id)}</span>
                  {task.duration_ms ? <span>{duration(task.duration_ms)}</span> : null}
                  {task.permission_denials ? <span className="warn">拒绝 {task.permission_denials}</span> : null}
                </div>
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {tab === 'events' ? (
        <div className="drawer-body">
          <div className="event-list live">
            {events.length === 0 ? <p className="drawer-empty">还没有 ACP 事件。执行任务后这里会滚动出现原始帧。</p> : null}
            {events
              .slice()
              .reverse()
              .map((event) => (
                <details key={event.key} className={`event-row lane-${event.lane}`}>
                  <summary>
                    <span className="event-kind">{event.title}</span>
                    <span className="event-time">{dateTime(event.at)}</span>
                  </summary>
                  <pre>{event.detail}</pre>
                </details>
              ))}
          </div>
        </div>
      ) : null}

      {tab === 'direct' ? (
        <div className="drawer-body">
          <div className="warning-box">
            直接命令绕过角色层：不判断意图、不改写、不总结。仍然经过权限确认和取消，仍然记录日志。
          </div>
          <textarea
            className="direct-input"
            rows={6}
            placeholder="例如：列出 workspace 里超过 1MB 的文件并告诉我名字"
            value={direct}
            onChange={(event) => setDirect(event.target.value)}
          />
          <button className="direct-send" onClick={() => void sendDirect()} disabled={sending || !direct.trim()}>
            {sending ? '发送中…' : '发给 Harness'}
          </button>
        </div>
      ) : null}
    </aside>
  );
}
