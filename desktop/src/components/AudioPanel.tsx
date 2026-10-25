import { useEffect, useState } from 'react';
import type { AssistantState, AudioDevice, EditableSettings } from '../types';

type Props = {
  apiBase: string;
  settings: EditableSettings;
  state: AssistantState | null;
  onSaved: () => void;
  onToast: (text: string) => void;
};

type Form = {
  inputDevice: string;
  vadThreshold: number;
  silenceMs: number;
  wakeEnabled: boolean;
  wakeThreshold: number;
  wakeScore: number;
};

function toForm(settings: EditableSettings): Form {
  return {
    inputDevice: settings.audio.input_device,
    vadThreshold: settings.recording.vad_threshold,
    silenceMs: settings.recording.silence_ms,
    wakeEnabled: settings.wake.enabled,
    wakeThreshold: settings.wake.threshold,
    wakeScore: settings.wake.score,
  };
}

export function AudioPanel({ apiBase, settings, state, onSaved, onToast }: Props) {
  const [form, setForm] = useState<Form>(() => toForm(settings));
  const [devices, setDevices] = useState<AudioDevice[]>(settings.audio.devices);
  const [level, setLevel] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setForm(toForm(settings));
    setDevices(settings.audio.devices);
  }, [settings]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void fetch(`${apiBase}/api/state`)
        .then((response) => response.json())
        .then((data: AssistantState) => setLevel(data.audio?.level ?? 0))
        .catch(() => undefined);
    }, 300);
    return () => window.clearInterval(timer);
  }, [apiBase]);

  const update = <K extends keyof Form>(key: K, value: Form[K]) => {
    setForm((previous) => ({ ...previous, [key]: value }));
  };

  const refreshDevices = async () => {
    try {
      const data = (await fetch(`${apiBase}/api/audio/devices`).then((response) => response.json())) as {
        devices: AudioDevice[];
      };
      setDevices(data.devices ?? []);
      onToast('设备列表已刷新');
    } catch (error) {
      onToast(`刷新失败：${String(error)}`);
    }
  };

  const save = async () => {
    setBusy(true);
    try {
      const response = await fetch(`${apiBase}/api/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input_device: form.inputDevice,
          vad_threshold: form.vadThreshold,
          silence_ms: form.silenceMs,
          wake_enabled: form.wakeEnabled,
          wake_threshold: form.wakeThreshold,
          wake_score: form.wakeScore,
        }),
      });
      const data = (await response.json()) as { ok?: boolean; detail?: string };
      if (!response.ok) {
        onToast(`保存失败：${data.detail ?? response.status}`);
        return;
      }
      onToast('音频设置已应用');
      onSaved();
    } catch (error) {
      onToast(`保存失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const meter = Math.min(1, level * 14);

  return (
    <section className="audio-panel">
      <div className="section-head">
        <div>
          <div className="setting-title">麦克风与识别</div>
          <div className="setting-hint">保存后立即生效；切换设备或唤醒阈值会短暂重启麦克风。</div>
        </div>
        <div className="row">
          <span className={`chip ${state?.voice_ready ? 'ok' : 'bad'}`}>
            麦克风 {state?.voice_ready ? '就绪' : '不可用'}
          </span>
          <span className={`chip ${state?.wake_ready ? 'ok' : 'bad'}`}>
            唤醒 {state?.wake_ready ? '就绪' : '关闭'}
          </span>
        </div>
      </div>

      <div className="setting-row">
        <div>
          <div className="setting-title">输入设备</div>
          <div className="setting-hint">
            默认跟随系统；也可以指定某个麦克风。修改后保存。
          </div>
        </div>
        <div className="row">
          <select
            className="select"
            value={form.inputDevice}
            onChange={(event) => update('inputDevice', event.target.value)}
          >
            <option value="">系统默认</option>
            {devices.map((device) => (
              <option key={`${device.index}-${device.name}`} value={device.name}>
                #{device.index} {device.name}
                {device.default ? '（默认）' : ''}
              </option>
            ))}
          </select>
          <button className="ghost" onClick={() => void refreshDevices()}>
            刷新
          </button>
        </div>
      </div>

      <div className="meter-test">
        <span className="mono-label">实时电平</span>
        <span className="meter big">
          <span className="meter-fill" style={{ width: `${Math.round(meter * 100)}%` }} />
        </span>
        <span className="mono-value">{level.toFixed(4)}</span>
      </div>

      <div className="slider-row">
        <div>
          <div className="setting-title">VAD 说话阈值</div>
          <div className="setting-hint">
            环境噪声大就调高；开始说话录不进去就调低。当前 {form.vadThreshold.toFixed(3)}。
          </div>
        </div>
        <input
          type="range"
          min={0.002}
          max={0.05}
          step={0.001}
          value={form.vadThreshold}
          onChange={(event) => update('vadThreshold', Number(event.target.value))}
        />
      </div>

      <div className="slider-row">
        <div>
          <div className="setting-title">说完后的静音判定</div>
          <div className="setting-hint">
            说话中停顿超过这个时间才算说完。当前 {(form.silenceMs / 1000).toFixed(1)} 秒。
          </div>
        </div>
        <input
          type="range"
          min={300}
          max={5000}
          step={100}
          value={form.silenceMs}
          onChange={(event) => update('silenceMs', Number(event.target.value))}
        />
      </div>

      <div className="setting-row">
        <div>
          <div className="setting-title">启用本地唤醒词</div>
          <div className="setting-hint">
            关闭后不监听唤醒词，但「开始听」和「按住说话」仍然可用。
          </div>
        </div>
        <button
          className={`switch gold ${form.wakeEnabled ? 'on' : ''}`}
          onClick={() => update('wakeEnabled', !form.wakeEnabled)}
        >
          <span />
        </button>
      </div>

      <div className="slider-row">
        <div>
          <div className="setting-title">唤醒灵敏阈值</div>
          <div className="setting-hint">
            越低越容易唤醒，也越容易误唤醒。当前 {form.wakeThreshold.toFixed(2)}。
          </div>
        </div>
        <input
          type="range"
          min={0.05}
          max={0.95}
          step={0.05}
          value={form.wakeThreshold}
          onChange={(event) => update('wakeThreshold', Number(event.target.value))}
        />
      </div>

      <div className="slider-row">
        <div>
          <div className="setting-title">唤醒词加权</div>
          <div className="setting-hint">越高越积极匹配唤醒词。当前 {form.wakeScore.toFixed(1)}。</div>
        </div>
        <input
          type="range"
          min={0.1}
          max={5}
          step={0.1}
          value={form.wakeScore}
          onChange={(event) => update('wakeScore', Number(event.target.value))}
        />
      </div>

      <div className="editor-foot">
        <button className="ghost" onClick={() => setForm(toForm(settings))}>
          还原
        </button>
        <button className="allow" disabled={busy} onClick={() => void save()}>
          {busy ? '应用中…' : '保存并应用'}
        </button>
      </div>
    </section>
  );
}
