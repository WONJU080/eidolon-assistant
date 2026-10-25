import type { AssistantState, CharacterConfig } from '../types';
import { STATE_LABELS, STATE_TONE } from '../lib';

type Props = {
  character: CharacterConfig | null;
  state: AssistantState | null;
  alwaysOnTop: boolean;
  onTogglePin: () => void;
  onSettings: () => void;
  onRaw: () => void;
  onHide: () => void;
};

export function TitleBar({ character, state, alwaysOnTop, onTogglePin, onSettings, onRaw, onHide }: Props) {
  const current = state?.state ?? 'idle';
  const tone = STATE_TONE[current] ?? 'idle';
  return (
    <header className="titlebar">
      <div className="titlebar-drag" />
      <div className="brand">
        <span className="brand-mark" style={{ background: character?.color ?? '#d4a24c' }} />
        <span className="brand-name">{character?.name_zh ?? 'Eidolon Assistant'}</span>
        <span className="brand-sub">{character?.name ?? ''}</span>
      </div>
      <div className={`status-pill tone-${tone}`}>
        <span className="status-dot" />
        <span>{STATE_LABELS[current] ?? current}</span>
        {state?.detail ? <span className="status-detail">{state.detail}</span> : null}
      </div>
      <div className="titlebar-actions">
        <button
          className={`icon-button ${alwaysOnTop ? 'active' : ''}`}
          title={alwaysOnTop ? '取消置顶' : '始终置顶'}
          onClick={onTogglePin}
        >
          ◉
        </button>
        <button className="icon-button raw" title="原始 Harness 面板" onClick={onRaw}>
          原始
        </button>
        <button className="icon-button" title="设置" onClick={onSettings}>
          ⚙
        </button>
        <button className="icon-button" title="隐藏窗口（继续常驻托盘）" onClick={onHide}>
          —
        </button>
      </div>
    </header>
  );
}
