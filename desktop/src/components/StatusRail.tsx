import type { AssistantState, Config } from '../types';

type Props = { state: AssistantState | null; config: Config | null };

const HARNESS_LABELS: Record<string, string> = {
  stopped: '已停止',
  starting: '启动中',
  ready: '就绪',
  busy: '执行中',
  error: '出错',
  killed: '已终止',
};

export function StatusRail({ state, config }: Props) {
  const harness = state?.harness;
  const level = Math.min(1, (state?.audio.level ?? 0) * 14);
  const auto = config?.harness.auto_approve;
  return (
    <section className="rail">
      <div className="rail-item">
        <span className="rail-label">Harness</span>
        <span className={`rail-value ${harness?.status === 'ready' ? 'ok' : harness?.status === 'error' ? 'bad' : ''}`}>
          {HARNESS_LABELS[harness?.status ?? ''] ?? harness?.status ?? '—'}
        </span>
        {harness?.pid ? <span className="rail-mono">pid {harness.pid}</span> : null}
      </div>
      <div className="rail-item">
        <span className="rail-label">权限</span>
        <span className={`rail-value ${auto ? 'bad' : 'ok'}`}>
          {auto ? '全部自动批准' : '高风险需确认'}
        </span>
      </div>
      <div className="rail-item">
        <span className="rail-label">唤醒词</span>
        <span className="rail-value">{config?.character.wake_words.join(' / ') ?? '—'}</span>
        <span className={`rail-dot ${state?.wake_ready ? 'on' : 'off'}`} title={state?.wake_ready ? '本地唤醒已就绪' : '本地唤醒未就绪'} />
      </div>
      <div className="rail-item rail-meter">
        <span className="rail-label">麦克风</span>
        <span className="meter">
          <span className="meter-fill" style={{ width: `${Math.round(level * 100)}%` }} />
        </span>
      </div>
      <div className="rail-spacer" />
      <div className="rail-item">
        <span className="rail-label">模型</span>
        <span className="rail-value">{config?.services.llm.model ?? '—'}</span>
      </div>
    </section>
  );
}
