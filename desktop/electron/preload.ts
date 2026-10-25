// Preload: the only bridge the renderer gets.
import { contextBridge, ipcRenderer } from 'electron';

export type AppInfo = {
  apiBase: string;
  version: string;
  projectRoot: string;
  dev: boolean;
  backendLog: string[];
  alwaysOnTop: boolean;
};

contextBridge.exposeInMainWorld('eidolon', {
  info: (): Promise<AppInfo> => ipcRenderer.invoke('app:info'),
  setAlwaysOnTop: (value: boolean): Promise<boolean> =>
    ipcRenderer.invoke('window:set-always-on-top', value),
  hide: (): Promise<void> => ipcRenderer.invoke('window:hide'),
  quit: (): Promise<void> => ipcRenderer.invoke('app:quit'),
  openPath: (target: string): Promise<string> => ipcRenderer.invoke('shell:open-path', target),
  openExternal: (target: string): Promise<void> => ipcRenderer.invoke('shell:open-external', target),
  onAction: (callback: (action: string) => void): (() => void) => {
    const handler = (_event: unknown, action: string): void => callback(action);
    ipcRenderer.on('action', handler);
    return () => ipcRenderer.removeListener('action', handler);
  },
  onBackendLog: (callback: (line: string) => void): (() => void) => {
    const handler = (_event: unknown, line: string): void => callback(line);
    ipcRenderer.on('backend:log', handler);
    return () => ipcRenderer.removeListener('backend:log', handler);
  },
  onAlwaysOnTop: (callback: (value: boolean) => void): (() => void) => {
    const handler = (_event: unknown, value: boolean): void => callback(value);
    ipcRenderer.on('window:always-on-top', handler);
    return () => ipcRenderer.removeListener('window:always-on-top', handler);
  },
});
