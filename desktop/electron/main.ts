// Electron main process: lifecycle, backend process, tray, global shortcuts, IPC.
import {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  globalShortcut,
  ipcMain,
  nativeImage,
  shell,
} from 'electron';
import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const here = __dirname;
const packaged = app.isPackaged;
const projectRoot =
  process.env.EIDOLON_PROJECT_ROOT || (packaged ? process.resourcesPath : path.resolve(here, '..', '..'));
const configDir =
  process.env.EIDOLON_CONFIG_DIR ||
  path.join(os.homedir(), 'Library', 'Application Support', 'EidolonAssistant', 'config');
const defaultConfigDir = path.join(projectRoot, 'config-default');
const devServerUrl = process.env.EIDOLON_DEV_SERVER_URL;
const isDev = Boolean(devServerUrl);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let backend: ChildProcess | null = null;
let apiBase = '';
let quitting = false;
let alwaysOnTop = false;
let healthTimer: NodeJS.Timeout | null = null;
const backendLog: string[] = [];

// --------------------------------------------------------------------- helpers
function log(message: string): void {
  const line = `[backend] ${message}`;
  console.log(line);
  backendLog.push(line);
  if (backendLog.length > 500) backendLog.shift();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('backend:log', line);
  }
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

function getJson(url: string, timeoutMs = 2500): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { timeout: timeoutMs }, (response) => {
      let data = '';
      response.on('data', (chunk) => (data += chunk));
      response.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on('error', reject);
    request.on('timeout', () => request.destroy(new Error('timeout')));
  });
}

function requestJson(method: string, url: string, body: unknown): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body ?? {});
    const target = new URL(url);
    const request = http.request(
      {
        method,
        hostname: target.hostname,
        port: target.port,
        path: target.pathname + target.search,
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
        timeout: 4000,
      },
      (response) => {
        let data = '';
        response.on('data', (chunk) => (data += chunk));
        response.on('end', () => {
          try {
            resolve(data ? JSON.parse(data) : {});
          } catch (error) {
            reject(error);
          }
        });
      },
    );
    request.on('error', reject);
    request.on('timeout', () => request.destroy(new Error('timeout')));
    request.write(payload);
    request.end();
  });
}

async function waitForHealth(timeoutMs = 60000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const health = (await getJson(`${apiBase}/api/health`, 1200)) as { ok?: boolean };
      if (health?.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`backend did not become healthy at ${apiBase}`);
}

// --------------------------------------------------------------------- backend
function pythonCandidates(): string[] {
  const candidates = [
    process.env.EIDOLON_PYTHON,
    path.join(projectRoot, '.venv', 'bin', 'python'),
    path.join(projectRoot, 'venv', 'bin', 'python'),
    '/usr/local/bin/python3',
    '/opt/homebrew/bin/python3',
    'python3',
  ].filter((value): value is string => Boolean(value));
  return candidates;
}

function seedConfig(): void {
  if (!fs.existsSync(defaultConfigDir)) return;
  if (fs.existsSync(path.join(configDir, 'settings.toml'))) return;
  fs.mkdirSync(configDir, { recursive: true });
  fs.cpSync(defaultConfigDir, configDir, {
    recursive: true,
    filter: (source) => !/local\.(env|toml)$/.test(source),
  });
  log(`seeded config into ${configDir}`);
}

function resolvePython(): string {
  for (const candidate of pythonCandidates()) {
    if (candidate === 'python3') return candidate;
    if (fs.existsSync(candidate)) return candidate;
  }
  return 'python3';
}

async function startBackend(): Promise<void> {
  const port = await freePort();
  apiBase = `http://127.0.0.1:${port}`;
  if (packaged) seedConfig();
  const python = resolvePython();
  const args = ['-m', 'eidolon_assistant', 'serve', '--port', String(port)];
  log(`starting ${python} ${args.join(' ')} (cwd=${projectRoot})`);
  const proc = spawn(python, args, {
    cwd: projectRoot,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      EIDOLON_PROJECT_ROOT: projectRoot,
      ...(packaged
        ? { EIDOLON_CONFIG_DIR: configDir, EIDOLON_DEFAULT_CONFIG_DIR: defaultConfigDir }
        : {}),
      PYTHONPATH: [path.join(projectRoot, 'backend'), process.env.PYTHONPATH]
        .filter(Boolean)
        .join(path.delimiter),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  backend = proc;
  proc.stdout?.on('data', (chunk: Buffer) => {
    for (const line of chunk.toString('utf8').split('\n')) {
      if (line.trim()) log(line);
    }
  });
  proc.stderr?.on('data', (chunk: Buffer) => {
    for (const line of chunk.toString('utf8').split('\n')) {
      if (line.trim()) log(line);
    }
  });
  proc.on('exit', (code, signal) => {
    log(`backend exited code=${code} signal=${signal}`);
    if (backend === proc) backend = null;
    if (!quitting && mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend:down', { code, signal });
    }
  });
  await waitForHealth();
}

async function stopBackend(): Promise<void> {
  if (healthTimer) {
    clearInterval(healthTimer);
    healthTimer = null;
  }
  const proc = backend;
  backend = null;
  if (!proc) return;
  // Ask the backend to tear down the harness, then the ACP child.
  try {
    const state = (await getJson(`${apiBase}/api/state`, 1500)) as {
      harness?: { pid?: number | null };
    };
    const harnessPid = state?.harness?.pid;
    if (harnessPid) {
      try {
        process.kill(-harnessPid, 'SIGTERM');
        log(`sent SIGTERM to harness group ${harnessPid}`);
      } catch {
        /* already gone */
      }
    }
  } catch {
    /* backend may be unreachable */
  }
  const exited = await new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => resolve(false), 10000);
    proc.once('exit', () => {
      clearTimeout(timer);
      resolve(true);
    });
    proc.kill('SIGTERM');
  });
  if (!exited) {
    log('backend did not exit; sending SIGKILL');
    try {
      proc.kill('SIGKILL');
    } catch {
      /* ignore */
    }
  }
}

// ---------------------------------------------------------------------- window
function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 760,
    minWidth: 460,
    minHeight: 560,
    show: false,
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 14, y: 16 },
    backgroundColor: '#0b0c10',
    vibrancy: 'under-window',
    visualEffectState: 'active',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  mainWindow.setAlwaysOnTop(alwaysOnTop, 'floating');
  mainWindow.once('ready-to-show', () => mainWindow?.show());
  mainWindow.on('close', (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on('enter-full-screen', () => mainWindow?.webContents.send('window:fullscreen', true));
  mainWindow.on('leave-full-screen', () => mainWindow?.webContents.send('window:fullscreen', false));
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  const capturePath = process.env.EIDOLON_CAPTURE;
  if (capturePath) {
    mainWindow.webContents.once('did-finish-load', () => {
      setTimeout(() => {
        void mainWindow?.webContents.capturePage().then((image) => {
          fs.writeFileSync(capturePath, image.toPNG());
          log(`captured window to ${capturePath}`);
        });
      }, 3500);
    });
  }
  if (devServerUrl) {
    void mainWindow.loadURL(devServerUrl);
  } else {
    void mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  }
}

function toggleWindow(): void {
  if (!mainWindow) {
    createWindow();
    return;
  }
  if (mainWindow.isVisible() && mainWindow.isFocused()) {
    mainWindow.hide();
  } else {
    mainWindow.show();
    mainWindow.focus();
  }
}

function sendAction(action: string): void {
  if (!mainWindow) createWindow();
  mainWindow?.show();
  mainWindow?.webContents.send('action', action);
}

// ------------------------------------------------------------------------ tray
function createTray(): void {
  const candidates = [
    path.join(projectRoot, 'assets', 'trayTemplate.png'),
    path.join(projectRoot, 'desktop', 'assets', 'trayTemplate.png'),
  ];
  const iconPath = candidates.find((candidate) => fs.existsSync(candidate)) ?? candidates[0];
  const image = nativeImage.createFromPath(iconPath);
  image.setTemplateImage(true);
  tray = new Tray(image);
  tray.setToolTip('Eidolon Assistant');
  const rebuild = (): void => {
    const menu = Menu.buildFromTemplate([
      { label: '显示 / 隐藏窗口', accelerator: 'CommandOrControl+Shift+E', click: toggleWindow },
      { label: '开始听', accelerator: 'CommandOrControl+Shift+L', click: () => sendAction('listen') },
      { label: '中断', accelerator: 'CommandOrControl+Shift+Escape', click: () => sendAction('cancel') },
      { type: 'separator' },
      {
        label: '总在最前',
        type: 'checkbox',
        checked: alwaysOnTop,
        click: (item) => {
          alwaysOnTop = item.checked;
          mainWindow?.setAlwaysOnTop(alwaysOnTop, 'floating');
          if (mainWindow) mainWindow.webContents.send('window:always-on-top', alwaysOnTop);
          rebuild();
        },
      },
      { label: '新会话（清空上下文）', click: () => sendAction('new-session') },
      { label: '打开工作目录', click: () => shell.openPath(path.join(process.env.HOME || '', 'Library/Application Support/EidolonAssistant/workspace')) },
      { label: '设置…', click: () => sendAction('settings') },
      { type: 'separator' },
      { label: '退出 Eidolon Assistant', click: () => { quitting = true; app.quit(); } },
    ]);
    tray?.setContextMenu(menu);
  };
  rebuild();
  tray.on('click', toggleWindow);
}

// ----------------------------------------------------------------- shortcuts
function registerShortcuts(): void {
  const shortcuts: Array<[string, () => void]> = [
    ['CommandOrControl+Shift+E', toggleWindow],
    ['CommandOrControl+Shift+L', () => sendAction('listen')],
    ['CommandOrControl+Shift+Escape', () => sendAction('cancel')],
  ];
  for (const [accelerator, handler] of shortcuts) {
    try {
      const ok = globalShortcut.register(accelerator, handler);
      if (!ok) log(`global shortcut busy: ${accelerator}`);
    } catch (error) {
      log(`global shortcut failed ${accelerator}: ${String(error)}`);
    }
  }
}

// ---------------------------------------------------------------------- menus
function createAppMenu(): void {
  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: 'Eidolon Assistant',
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { label: '设置…', accelerator: 'CommandOrControl+,', click: () => sendAction('settings') },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { type: 'separator' },
        { label: '退出', accelerator: 'CommandOrControl+Q', click: () => { quitting = true; app.quit(); } },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo' }, { role: 'redo' }, { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
      ],
    },
    {
      label: '窗口',
      submenu: [
        { role: 'minimize' }, { role: 'zoom' }, { type: 'separator' },
        { label: '显示 / 隐藏', accelerator: 'CommandOrControl+Shift+E', click: toggleWindow },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ------------------------------------------------------------------- IPC setup
function setupIpc(): void {
  ipcMain.handle('app:info', () => ({
    apiBase,
    version: app.getVersion(),
    projectRoot,
    dev: isDev,
    backendLog: backendLog.slice(-200),
    alwaysOnTop,
  }));
  ipcMain.handle('window:set-always-on-top', (_event, value: boolean) => {
    alwaysOnTop = Boolean(value);
    mainWindow?.setAlwaysOnTop(alwaysOnTop, 'floating');
    return alwaysOnTop;
  });
  ipcMain.handle('window:hide', () => mainWindow?.hide());
  ipcMain.handle('app:quit', () => {
    quitting = true;
    app.quit();
  });
  ipcMain.handle('shell:open-path', (_event, target: string) => shell.openPath(target));
  ipcMain.handle('shell:open-external', (_event, target: string) => shell.openExternal(target));
}

// ---------------------------------------------------------------------- boot
const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

app.whenReady().then(async () => {
  setupIpc();
  createAppMenu();
  try {
    await startBackend();
  } catch (error) {
    dialog.showMessageBox({
      type: 'error',
      title: 'Eidolon Assistant',
      message: '后端没有启动',
      detail:
        `${String(error)}\n\n请先在项目目录运行:\n` +
        `python3 -m venv .venv && .venv/bin/pip install -e backend`,
      buttons: ['好'],
    });
  }
  createWindow();
  createTray();
  registerShortcuts();

  // keep the renderer informed when the backend crashes later
  healthTimer = setInterval(async () => {
    if (!backend) return;
    try {
      await getJson(`${apiBase}/api/health`, 1500);
    } catch {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('backend:unhealthy');
      }
    }
  }, 5000);
});

app.on('activate', () => {
  if (mainWindow) mainWindow.show();
  else createWindow();
});

app.on('window-all-closed', () => {
  // stay alive in the tray; quitting is explicit
});

app.on('before-quit', () => {
  quitting = true;
});

// Terminal launches (and `kill -TERM`) must clean the harness child too.
for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => {
    if (quitting) return;
    quitting = true;
    void stopBackend().finally(() => app.exit(0));
  });
}

app.on('will-quit', async (event) => {
  event.preventDefault();
  globalShortcut.unregisterAll();
  await stopBackend();
  app.exit(0);
});
