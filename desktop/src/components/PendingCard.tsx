import { useEffect, useState } from 'react';
import type { PermissionRequest } from '../types';
import { clip } from '../lib';

type Pending =
  | { id: string; kind: 'task'; ja: string; zh: string; task: string }
  | { id: string; kind: 'permission'; ja: string; zh: string; request: PermissionRequest };

type Props = {
  pending: Pending;
  timeoutSeconds: number;
  onAnswer: (allow: boolean) => void;
};

export function PendingCard({ pending, timeoutSeconds, onAnswer }: Props) {
  const [showRaw, setShowRaw] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    setElapsed(0);
    const timer = setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [pending.id]);
  const remaining = Math.max(0, timeoutSeconds - elapsed);
  const ratio = Math.max(0, Math.min(1, remaining / timeoutSeconds));
  return (
    <div className="pending-card">
      <div className="pending-head">
        <span className="pending-kind">{pending.kind === 'permission' ? '需要确认的操作' : '要执行的任务'}</span>
        <span className="pending-count">{remaining}s</span>
      </div>
      <div className="pending-ja">{pending.ja}</div>
      <div className="pending-zh">{pending.zh}</div>
      {pending.kind === 'permission' ? (
        <div className="pending-detail">
          <div className="pending-command">
            <span className="mono-label">命令</span>
            <code>{clip(pending.request.command || pending.request.title, 220)}</code>
          </div>
          {pending.request.justification ? (
            <div className="pending-justification">理由：{clip(pending.request.justification, 220)}</div>
          ) : null}
          <button className="link" onClick={() => setShowRaw((value) => !value)}>
            {showRaw ? '收起原始请求' : '查看原始权限请求'}
          </button>
          {showRaw ? (
            <pre className="raw-json">{JSON.stringify(pending.request, null, 2)}</pre>
          ) : null}
        </div>
      ) : (
        <div className="pending-detail">
          <div className="pending-justification">任务：{clip(pending.task, 200)}</div>
        </div>
      )}
      <div className="pending-actions">
        <button className="deny" onClick={() => onAnswer(false)}>
          拒绝
        </button>
        <button className="allow" onClick={() => onAnswer(true)}>
          {pending.kind === 'permission' ? '允许一次' : '确认执行'}
        </button>
      </div>
      <div className="pending-bar">
        <span style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}
