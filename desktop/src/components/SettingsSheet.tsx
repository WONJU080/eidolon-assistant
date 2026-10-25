import { useEffect, useState } from 'react';
import type { AssistantState, CharacterMeta, Config, EditableSettings } from '../types';
import { AudioPanel } from './AudioPanel';
import { CharacterPanel } from './CharacterPanel';

type Props = {
  open: boolean;
  onClose: () => void;
  apiBase: string;
  config: Config | null;
  state: AssistantState | null;
  alwaysOnTop: boolean;
  onTogglePin: (value: boolean) => void;
  onRefresh: () => void;
  onToast: (text: string) => void;
  backendLog: string[];
  characters: CharacterMeta[];
  activeId: string;
  editable: EditableSettings | null;
  onRefreshCharacters: () => void;
  onRefreshSettings: () => void;
};

type Diagnostics = {
  dsh: { command: string; exists: boolean; profile: string };
  permission_mode: string;
  workspace: string;
  audio: { input_devices: Array<{ index: number; name: string; default: boolean }>; configured_input: string; sample_rate: number; vad_threshold: number };
  models: { kws_dir: string; kws_exists: boolean };
  services: Record<string, { model: string; key: boolean; base_url?: string; endpoint?: string }>;
  proxy: { http: string | null; https: string | null; socks: string | null };
  character: { id: string; wake_words: string[] };
  paths: { data: string; db: string; logs: string };
};

type Tab = 'character' | 'audio' | 'permission' | 'diagnostics';

export function SettingsSheet({
  open,
  onClose,
  apiBase,
  config,
  state,
  alwaysOnTop,
  onTogglePin,
  onRefresh,
  onToast,
  backendLog,
  characters,
  activeId,
  editable,
  onRefreshCharacters,
  onRefreshSettings,
}: Props) {
  const [tab, setTab] = useState<Tab>('character');
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [pendingMode, setPendingMode] = useState(false);
  const [confirmText, setConfirmText] = useState('');

  useEffect(() => {
    if (!open) return;
    onRefreshCharacters();
    onRefreshSettings();
    void fetch(`${apiBase}/api/diagnostics`)
      .then((response) => response.json())
      .then((data: Diagnostics) => setDiagnostics(data))
      .catch(() => setDiagnostics(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, apiBase]);

  if (!open) return null;
  const auto = config?.harness.auto_approve ?? false;

  const toggleAuto = async () => {
    if (auto) {
      await applyAuto(false);
      return;
    }
    setPendingMode(true);
    setConfirmText('');
  };

  const applyAuto = async (enabled: boolean) => {
    setPendingMode(false);
    try {
      const response = await fetch(`${apiBase}/api/auto-approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      const data = (await response.json()) as { ok: boolean; error?: string };
      if (data.ok) {
        onToast(enabled ? '已开启全部自动批准（危险）' : '已恢复高风险确认');
        onRefresh();
      } else {
        onToast(`切换失败：${data.error ?? '未知原因'}`);
      }
    } catch (error) {
      onToast(`切换失败：${String(error)}`);
    }
  };

  const newSession = async () => {
    try {
      const response = await fetch(`${apiBase}/api/session/new`, { method: 'POST' });
      const data = (await response.json()) as { ok: boolean; session?: string; error?: string };
      onToast(data.ok ? `已开启新会话 ${data.session?.slice(0, 8) ?? ''}` : `失败：${data.error}`);
    } catch (error) {
      onToast(`失败：${String(error)}`);
    }
  };

  const openWorkspace = () => {
    if (diagnostics?.workspace) void window.eidolon?.openPath(diagnostics.workspace);
  };

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet wide" onClick={(event) => event.stopPropagation()}>
        <div className="sheet-head">
          <div>
            <div className="drawer-title">设置与诊断</div>
            <div className="drawer-sub">
              当前角色：{characters.find((item) => item.id === activeId)?.name_zh ?? activeId}
              · 密钥只存在本地
            </div>
          </div>
          <button className="icon-button" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="settings-tabs">
          <button className={tab === 'character' ? 'active' : ''} onClick={() => setTab('character')}>
            角色
          </button>
          <button className={tab === 'audio' ? 'active' : ''} onClick={() => setTab('audio')}>
            麦克风与唤醒
          </button>
          <button className={tab === 'permission' ? 'active' : ''} onClick={() => setTab('permission')}>
            权限与窗口
          </button>
          <button className={tab === 'diagnostics' ? 'active' : ''} onClick={() => setTab('diagnostics')}>
            诊断
          </button>
        </div>

        <div className="sheet-body">
          {tab === 'character' ? (
            <CharacterPanel
              apiBase={apiBase}
              characters={characters}
              activeId={activeId}
              onToast={onToast}
              onChanged={() => {
                onRefreshCharacters();
                onRefreshSettings();
                onRefresh();
              }}
            />
          ) : null}

          {tab === 'audio' ? (
            editable ? (
              <AudioPanel
                apiBase={apiBase}
                settings={editable}
                state={state}
                onToast={onToast}
                onSaved={() => {
                  onRefreshSettings();
                  onRefresh();
                }}
              />
            ) : (
              <p className="drawer-empty">正在读取音频设置…</p>
            )
          ) : null}

          {tab === 'permission' ? (
            <>
              <section className="sheet-section">
                <h3>权限策略</h3>
                <div className="setting-row">
                  <div>
                    <div className="setting-title">全部自动批准</div>
                    <div className="setting-hint">
                      关闭时：写文件、删除、执行需要写权限的 shell 都要你确认。开启后 Harness 不再询问。
                    </div>
                  </div>
                  <button className={`switch ${auto ? 'on' : ''}`} onClick={() => void toggleAuto()}>
                    <span />
                  </button>
                </div>
                {auto ? (
                  <div className="danger-banner">
                    警告：全部自动批准已开启。Harness 可以在工作目录内外执行写入和命令，不再逐次询问。
                  </div>
                ) : null}
                {pendingMode ? (
                  <div className="confirm-banner">
                    <span>确定要让 Harness 不再逐次询问吗？</span>
                    <input
                      className="confirm-input"
                      placeholder="输入“确认”"
                      value={confirmText}
                      onChange={(event) => setConfirmText(event.target.value)}
                    />
                    <button
                      className="deny"
                      disabled={confirmText.trim() !== '确认'}
                      onClick={() => void applyAuto(true)}
                    >
                      确定开启
                    </button>
                    <button className="ghost" onClick={() => setPendingMode(false)}>
                      取消
                    </button>
                  </div>
                ) : null}
              </section>

              <section className="sheet-section">
                <h3>窗口</h3>
                <div className="setting-row">
                  <div>
                    <div className="setting-title">始终置顶</div>
                    <div className="setting-hint">悬浮在其他窗口之上；也可以从托盘菜单切换。</div>
                  </div>
                  <button className={`switch ${alwaysOnTop ? 'on' : ''}`} onClick={() => onTogglePin(!alwaysOnTop)}>
                    <span />
                  </button>
                </div>
                <div className="setting-row">
                  <div>
                    <div className="setting-title">新会话（清空上下文）</div>
                    <div className="setting-hint">关闭当前 ACP 会话，新建一个没有上下文的会话。</div>
                  </div>
                  <button className="ghost" onClick={() => void newSession()}>
                    新会话
                  </button>
                </div>
              </section>
            </>
          ) : null}

          {tab === 'diagnostics' ? (
            <>
              <section className="sheet-section">
                <h3>服务</h3>
                {config
                  ? (Object.entries(config.services) as Array<[string, { model: string; ready: boolean }]>).map(([key, value]) => (
                      <div className="kv" key={key}>
                        <span>{key === 'wake' ? '唤醒' : key.toUpperCase()}</span>
                        <span className={value.ready ? 'ok' : 'bad'}>
                          {value.model ?? '—'} {value.ready ? '· 密钥就绪' : '· 缺少密钥'}
                        </span>
                      </div>
                    ))
                  : null}
                <div className="kv">
                  <span>Harness</span>
                  <span className={diagnostics?.dsh.exists ? 'ok' : 'bad'}>
                    {diagnostics?.dsh.exists ? 'dsh CLI 可用' : '找不到 dsh CLI'}
                  </span>
                </div>
                {diagnostics?.proxy.https ? (
                  <div className="kv">
                    <span>系统代理</span>
                    <span>{diagnostics.proxy.https}</span>
                  </div>
                ) : null}
              </section>

              <section className="sheet-section">
                <h3>工作目录</h3>
                <div className="mono-block">{diagnostics?.workspace ?? '—'}</div>
                <div className="row">
                  <button className="ghost" onClick={openWorkspace}>
                    在访达中打开
                  </button>
                  <button
                    className="ghost"
                    onClick={() => void window.eidolon?.openPath(diagnostics?.paths.data ?? '')}
                  >
                    打开数据目录
                  </button>
                </div>
                <div className="setting-hint">
                  Harness 的 cwd 就是这个工作目录；权限为 read-only 时，写它以外的位置也会要求确认。
                </div>
              </section>

              {diagnostics ? (
                <section className="sheet-section">
                  <h3>麦克风设备</h3>
                  {diagnostics.audio.input_devices.map((device) => (
                    <div className="kv" key={device.index}>
                      <span>#{device.index}</span>
                      <span>
                        {device.name} {device.default ? '· 默认' : ''}
                      </span>
                    </div>
                  ))}
                </section>
              ) : null}

              <section className="sheet-section">
                <h3>后端日志</h3>
                <pre className="log-tail">{backendLog.slice(-40).join('\n') || '（暂无）'}</pre>
              </section>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
