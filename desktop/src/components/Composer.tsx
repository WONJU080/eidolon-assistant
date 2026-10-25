import { useRef, useState } from 'react';
import type { AssistantState } from '../types';

type Props = {
  state: AssistantState | null;
  busy: boolean;
  onSend: (text: string) => void;
  onListen: () => void;
  onPttStart: () => void;
  onPttStop: () => void;
  onCancel: () => void;
  ttsSpeaking: boolean;
  allowText: boolean;
};

export function Composer({ state, busy, onSend, onListen, onPttStart, onPttStop, onCancel, ttsSpeaking, allowText }: Props) {
  const [text, setText] = useState('');
  const [pressing, setPressing] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const voiceOff = !state?.voice_ready;

  const submit = () => {
    const value = text.trim();
    if (!value) return;
    onSend(value);
    setText('');
    if (area.current) area.current.style.height = 'auto';
  };

  const hold = () => {
    if (pressing) return;
    setPressing(true);
    onPttStart();
  };
  const release = () => {
    if (!pressing) return;
    setPressing(false);
    onPttStop();
  };

  return (
    <footer className="composer">
      <div className="composer-row">
        <textarea
          ref={area}
          className="composer-input"
          placeholder={
            state?.state === 'waiting_answer'
              ? '请补充信息：直接说，或者打字后回车'
              : state?.state === 'waiting_confirmation'
                ? '回复「可以」/「不要」，或点上面的按钮'
                : busy
                  ? '正在忙…（按中断停止）'
                  : '对她说点什么，或按住「说话」'
          }
          value={text}
          rows={1}
          onChange={(event) => {
            setText(event.target.value);
            const target = event.target;
            target.style.height = 'auto';
            target.style.height = `${Math.min(120, target.scrollHeight)}px`;
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button className="send" onClick={submit} disabled={!text.trim() || (busy && !allowText)} title="发送（Enter）">
          发送
        </button>
      </div>
      <div className="composer-actions">
        <button className="ghost" onClick={onListen} disabled={busy || voiceOff} title="开始听，说完自动停止（全局快捷键 ⌘⇧L）">
          ◉ 开始听
        </button>
        <button
          className={`ghost hold ${pressing ? 'holding' : ''}`}
          onPointerDown={hold}
          onPointerUp={release}
          onPointerLeave={release}
          onPointerCancel={release}
          disabled={busy || voiceOff}
          title="按住说话"
        >
          {pressing ? '● 松开发送' : '按住说话'}
        </button>
        <div className="grow" />
        {state?.state === 'waiting_confirmation' ? <span className="wait-hint">在等你的回答…</span> : null}
        <button
          className={`interrupt ${busy || ttsSpeaking ? 'live' : ''}`}
          onClick={onCancel}
          disabled={!busy && !ttsSpeaking}
          title="中断（⌘⇧Esc）"
        >
          ■ 中断
        </button>
      </div>
    </footer>
  );
}
