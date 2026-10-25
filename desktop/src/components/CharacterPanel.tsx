import { useEffect, useRef, useState } from 'react';
import type { CharacterDetail, CharacterMeta } from '../types';

type FormState = {
  name: string;
  name_zh: string;
  name_romaji: string;
  description: string;
  color: string;
  voice_id: string;
  tts_language: string;
  wake_words: string;
  wake_reply_tts: string;
  wake_reply_zh: string;
  sheet_text: string;
};

type Props = {
  apiBase: string;
  characters: CharacterMeta[];
  activeId: string;
  onChanged: () => void;
  onToast: (text: string) => void;
};

function toForm(character: CharacterDetail): FormState {
  return {
    name: character.name,
    name_zh: character.name_zh,
    name_romaji: character.name_romaji,
    description: character.description,
    color: character.color,
    voice_id: character.voice_id,
    tts_language: character.tts_language,
    wake_words: character.wake_words.join('，'),
    wake_reply_tts: character.wake_reply_tts,
    wake_reply_zh: character.wake_reply_zh,
    sheet_text: character.sheet_text,
  };
}

function parseWords(value: string): string[] {
  return value
    .split(/[,，、\s]+/)
    .map((word) => word.trim())
    .filter(Boolean);
}

export function CharacterPanel({ apiBase, characters, activeId, onChanged, onToast }: Props) {
  const [selectedId, setSelectedId] = useState(activeId || characters[0]?.id || '');
  const [detail, setDetail] = useState<CharacterDetail | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (characters.length === 0) return;
    if (!characters.some((item) => item.id === selectedId)) {
      setSelectedId(activeId || characters[0].id);
    }
  }, [characters, activeId, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    void fetch(`${apiBase}/api/characters/${selectedId}`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data: { character: CharacterDetail }) => {
        if (cancelled) return;
        setDetail(data.character);
        setForm(toForm(data.character));
      })
      .catch(() => {
        if (!cancelled) onToast('读取角色失败');
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, selectedId, onToast]);

  const update = (key: keyof FormState, value: string) => {
    setForm((previous) => (previous ? { ...previous, [key]: value } : previous));
  };

  const save = async () => {
    if (!form || !detail || busy) return;
    setBusy(true);
    try {
      const response = await fetch(`${apiBase}/api/characters/${detail.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...form,
          wake_words: parseWords(form.wake_words),
        }),
      });
      const data = (await response.json()) as { ok?: boolean; detail?: string; applied?: boolean; warning?: string };
      if (!response.ok) {
        onToast(`保存失败：${data.detail ?? response.status}`);
        return;
      }
      const missingVoice = form.voice_id.trim() ? '' : '；没有 voice_id，语音会失败';
      onToast(data.warning ? `已保存；${data.warning}` : `角色设置已保存${missingVoice}`);
      onChanged();
    } catch (error) {
      onToast(`保存失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const activate = async (id: string) => {
    setBusy(true);
    try {
      const response = await fetch(`${apiBase}/api/characters/${id}/activate`, { method: 'POST' });
      const data = (await response.json()) as { ok?: boolean; detail?: string };
      if (!response.ok) {
        onToast(`切换失败：${data.detail ?? response.status}`);
        return;
      }
      setSelectedId(id);
      onToast('角色已切换');
      onChanged();
    } catch (error) {
      onToast(`切换失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm('确定删除这个角色包？此操作不可撤销。')) return;
    setBusy(true);
    try {
      const response = await fetch(`${apiBase}/api/characters/${id}`, { method: 'DELETE' });
      const data = (await response.json()) as { ok?: boolean; detail?: string };
      if (!response.ok) {
        onToast(`删除失败：${data.detail ?? response.status}`);
        return;
      }
      onToast('角色已删除');
      onChanged();
    } catch (error) {
      onToast(`删除失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const exportPackage = async (id: string) => {
    try {
      const response = await fetch(`${apiBase}/api/characters/${id}/export`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${id}.eidolon`;
      anchor.click();
      URL.revokeObjectURL(url);
      onToast('已导出 .eidolon');
    } catch (error) {
      onToast(`导出失败：${String(error)}`);
    }
  };

  const importFile = async (fileValue: File, overwrite = false) => {
    setBusy(true);
    try {
      const body = new FormData();
      body.append('file', fileValue);
      const response = await fetch(
        `${apiBase}/api/characters/import${overwrite ? '?overwrite=true' : ''}`,
        { method: 'POST', body },
      );
      if (response.status === 409 && !overwrite) {
        if (window.confirm('同名角色已存在，要覆盖吗？')) {
          await importFile(fileValue, true);
        }
        return;
      }
      const data = (await response.json()) as { ok?: boolean; detail?: string; character?: CharacterMeta };
      if (!response.ok) {
        onToast(`导入失败：${data.detail ?? response.status}`);
        return;
      }
      const warning = data.character && !data.character.voice_id
        ? '；这个角色还没有 voice_id，语音会失败'
        : '';
      onToast(`已导入 ${data.character?.name_zh ?? ''}${warning}`);
      if (data.character) setSelectedId(data.character.id);
      onChanged();
    } catch (error) {
      onToast(`导入失败：${String(error)}`);
    } finally {
      setBusy(false);
      if (file.current) file.current.value = '';
    }
  };

  const preview = async () => {
    if (!detail?.voice_id) {
      onToast('这个角色还没有 voice_id');
      return;
    }
    setPreviewing(true);
    try {
      const text = form?.wake_reply_tts || '……わかった。';
      const response = await fetch(`${apiBase}/api/characters/${detail.id}/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!response.ok) {
        const data = (await response.json()) as { detail?: string };
        onToast(`试听失败：${data.detail ?? response.status}`);
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      await audio.play();
    } catch (error) {
      onToast(`试听失败：${String(error)}`);
    } finally {
      setPreviewing(false);
    }
  };

  const avatarUrl = detail?.avatar ? `${apiBase}/api/characters/${detail.id}/avatar` : '';

  return (
    <section className="character-panel">
      <div className="section-head">
        <div>
          <div className="setting-title">角色包</div>
          <div className="setting-hint">
            每个角色一个目录，包含 character.toml、sheet.md、可选头像。导入/导出格式为
            <code>.eidolon</code>（zip）。
          </div>
        </div>
        <div className="row">
          <button className="ghost" disabled={busy} onClick={() => file.current?.click()}>
            导入 .eidolon
          </button>
          <input
            ref={file}
            type="file"
            accept=".eidolon,.zip,application/zip"
            hidden
            onChange={(event) => {
              const chosen = event.target.files?.[0];
              if (chosen) void importFile(chosen);
            }}
          />
        </div>
      </div>

      <div className="character-layout">
        <div className="character-list">
          {characters.map((character) => (
            <button
              key={character.id}
              className={`character-row ${character.id === selectedId ? 'selected' : ''}`}
              onClick={() => setSelectedId(character.id)}
            >
              <span className="avatar" style={{ background: character.color }}>
                {character.avatar ? (
                  <img src={`${apiBase}/api/characters/${character.id}/avatar`} alt="" />
                ) : (
                  character.name_zh.slice(0, 1)
                )}
              </span>
              <span className="character-meta">
                <span className="character-name">
                  {character.name_zh}
                  {character.id === activeId ? <span className="active-chip">当前</span> : null}
                </span>
                <span className="character-sub">
                  {character.wake_words.join(' / ') || '没有唤醒词'}
                </span>
              </span>
            </button>
          ))}
        </div>

        <div className="character-editor">
          {form && detail ? (
            <>
              <div className="editor-head">
                <div className="editor-title">
                  {detail.avatar ? <img className="editor-avatar" src={avatarUrl} alt="" /> : null}
                  {detail.name_zh}
                  {detail.id === activeId ? <span className="active-chip">当前角色</span> : null}
                </div>
                <div className="row">
                  {detail.id !== activeId ? (
                    <button className="allow" disabled={busy} onClick={() => void activate(detail.id)}>
                      切换到此角色
                    </button>
                  ) : null}
                  <button className="ghost" onClick={() => void exportPackage(detail.id)}>
                    导出
                  </button>
                  <button
                    className="deny"
                    disabled={busy || detail.id === activeId}
                    title={detail.id === activeId ? '先切换到别的角色再删除' : '删除角色包'}
                    onClick={() => void remove(detail.id)}
                  >
                    删除
                  </button>
                </div>
              </div>

              <div className="form-grid">
                <label>
                  <span>中文名</span>
                  <input value={form.name_zh} onChange={(event) => update('name_zh', event.target.value)} />
                </label>
                <label>
                  <span>原名 / 日文名</span>
                  <input value={form.name} onChange={(event) => update('name', event.target.value)} />
                </label>
                <label>
                  <span>罗马字</span>
                  <input value={form.name_romaji} onChange={(event) => update('name_romaji', event.target.value)} />
                </label>
                <label>
                  <span>颜色</span>
                  <span className="color-row">
                    <input
                      type="color"
                      value={/^#[0-9a-fA-F]{6}$/.test(form.color) ? form.color : '#d4a24c'}
                      onChange={(event) => update('color', event.target.value)}
                    />
                    <input value={form.color} onChange={(event) => update('color', event.target.value)} />
                  </span>
                </label>
                <label className="wide">
                  <span>一句话介绍</span>
                  <input value={form.description} onChange={(event) => update('description', event.target.value)} />
                </label>
                <label className="wide">
                  <span>Voice ID（Fish Audio reference id）</span>
                  <span className="row">
                    <input value={form.voice_id} onChange={(event) => update('voice_id', event.target.value)} />
                    <button className="ghost" disabled={previewing} onClick={() => void preview()}>
                      {previewing ? '合成中…' : '试听'}
                    </button>
                  </span>
                </label>
                <label>
                  <span>台词语言</span>
                  <select
                    value={form.tts_language}
                    onChange={(event) => update('tts_language', event.target.value)}
                  >
                    <option value="ja">日语（TTS 读日语，显示中文字幕）</option>
                    <option value="zh">中文</option>
                  </select>
                </label>
                <label>
                  <span>唤醒词（中文，逗号分隔）</span>
                  <input
                    value={form.wake_words}
                    onChange={(event) => update('wake_words', event.target.value)}
                  />
                </label>
                <label>
                  <span>唤醒回应（TTS 文本）</span>
                  <input
                    value={form.wake_reply_tts}
                    onChange={(event) => update('wake_reply_tts', event.target.value)}
                  />
                </label>
                <label>
                  <span>唤醒回应（中文字幕）</span>
                  <input
                    value={form.wake_reply_zh}
                    onChange={(event) => update('wake_reply_zh', event.target.value)}
                  />
                </label>
                <label className="wide">
                  <span>人设 sheet（sheet.md）</span>
                  <textarea
                    rows={10}
                    value={form.sheet_text}
                    onChange={(event) => update('sheet_text', event.target.value)}
                  />
                </label>
              </div>
              <div className="editor-foot">
                <span className="setting-hint">
                  唤醒词必须是中文汉字（本地 KWS 用拼音）；保存当前角色会立即重建唤醒词与声音。
                </span>
                <button className="allow" disabled={busy} onClick={() => void save()}>
                  {busy ? '保存中…' : '保存角色设置'}
                </button>
              </div>
            </>
          ) : (
            <p className="drawer-empty">选择一个角色来编辑。</p>
          )}
        </div>
      </div>
    </section>
  );
}
