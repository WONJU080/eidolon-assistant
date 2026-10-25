import { useEffect, useRef } from 'react';
import type { Activity, Message } from '../types';
import { clip, sourceLabel, timeOfDay } from '../lib';

type Props = {
  messages: Message[];
  activity: Activity[];
  characterName: string;
  wakeWords: string[];
  onExample: (text: string) => void;
  scrollSignal?: string;
};

const EXAMPLES = [
  '帮我看看明天北京天气',
  '列出工作目录里的文件',
  '我今天有点累',
];

export function Transcript({ messages, activity, characterName, wakeWords, onExample, scrollSignal }: Props) {
  const bottom = useRef<HTMLDivElement>(null);
  const hasConversation = messages.some((message) => message.role !== 'system');
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, scrollSignal]);

  return (
    <section className="transcript">
      {activity.length > 0 ? (
        <div className="activity-row">
          {activity.slice(-4).map((item) => (
            <span key={item.key} className={`activity-chip tone-${item.tone ?? 'plain'}`} title={item.label}>
              {clip(item.label, 42)}
            </span>
          ))}
        </div>
      ) : null}
      <div className="messages">
        {messages.map((message) => {
          if (message.role === 'system') {
            return (
              <div key={message.key} className={`system-line level-${message.level ?? 'info'}`}>
                <span className="system-time">{timeOfDay(message.at)}</span>
                <span>{message.text}</span>
              </div>
            );
          }
          if (message.role === 'user') {
            return (
              <div key={message.key} className="bubble user">
                <div className="bubble-text">{message.text}</div>
                <div className="bubble-meta">
                  <span>{sourceLabel(message.source)}</span>
                  <span>{timeOfDay(message.at)}</span>
                </div>
              </div>
            );
          }
          return (
            <div key={message.key} className="bubble assistant">
              <div className="bubble-speaker">
                {characterName}
                {message.tag === 'summary' ? <span className="tag">任务汇报</span> : null}
                {message.tag === 'clarify' ? <span className="tag">追问</span> : null}
                {message.tag === 'permission' ? <span className="tag">权限</span> : null}
              </div>
              <div className="bubble-ja">{message.ja ?? message.text}</div>
              {message.zh && message.zh !== message.ja ? <div className="bubble-zh">{message.zh}</div> : null}
              <div className="bubble-meta">
                <span>{timeOfDay(message.at)}</span>
                {message.speak === false ? <span className="muted">未朗读</span> : null}
              </div>
            </div>
          );
        })}
      </div>
      {!hasConversation ? (
        <div className="empty-state">
          <p className="empty-line ja">……呼んだ？</p>
          <p className="empty-line zh">叫我的话，说「{wakeWords[0] ?? '拉姆'}」就行。</p>
          <p className="empty-hint">或者直接打字。需要动电脑的事，我会先确认再交给 Harness。</p>
          <div className="examples">
            {EXAMPLES.map((text) => (
              <button key={text} className="example" onClick={() => onExample(text)}>
                {text}
              </button>
            ))}
          </div>
        </div>
      ) : null}
      <div ref={bottom} />
    </section>
  );
}
