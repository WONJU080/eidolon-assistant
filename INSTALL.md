# 安装与首次运行

这份文档面向第一次接触本项目的人。按顺序执行即可；如果某一步失败，先看最后的
[故障排查](#故障排查)。

> 这是源码安装，不是双击 `.app` 的发行版。你需要使用“终端”（Terminal）。

## 0. 确认你的电脑

- macOS 13 或更新版本。
- 推荐 Apple Silicon（M1/M2/M3/M4）；Intel Mac 可以尝试，但本项目主要在 Apple Silicon 上验证。
- 至少 2GB 可用磁盘空间。
- 可以访问 GitHub、DeepSeek、阿里云百炼、Fish Audio 的网络。
- 一个 DeepSeek Harness 桌面版账号。

## 1. 安装 Python 3.12+

先检查：

```bash
python3 --version
```

如果低于 3.12，或没有安装：

- 打开 <https://www.python.org/downloads/macos/>，安装 Python 3.12 或 3.13；
- 或者使用 Homebrew：

```bash
brew install python@3.13
```

安装后重新打开 Terminal，再执行 `python3 --version`。

## 2. 安装 Node.js 20+

```bash
node --version
npm --version
```

如果低于 20，或没有安装：

- 打开 <https://nodejs.org/> 下载 LTS 版本；
- 或使用 Homebrew：

```bash
brew install node
```

## 3. 安装并登录 DeepSeek Harness

本项目不包含 DeepSeek Harness，需要单独安装桌面版：

1. 安装 DeepSeek Harness 桌面版；
2. 打开它并完成登录；
3. 确认 CLI 存在：

```bash
ls "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
```

如果路径不同，编辑 `config/settings.toml` 中的：

```toml
[harness]
command = "/你的/dsh/路径"
```

没有 Harness 时，闲聊和语音仍可能工作，但“操作电脑”的任务无法执行。

## 4. 下载项目

```bash
git clone https://github.com/WONJU080/eidolon-assistant.git eidolon-assistant
cd eidolon-assistant
```

如果下载的是 zip，解压后进入 `eidolon-assistant` 目录。

## 5. 运行安装脚本

```bash
./scripts/setup.sh
```

它会依次：

1. 检查 macOS / Python / Node；
2. 创建 `.venv` 并安装 backend 依赖；
3. 安装 `desktop/node_modules`；
4. 构建 Electron main + React renderer；
5. 下载 KWS 模型（约 36MB）到 `models/`；
6. 从 `config/local.env.example` 创建 `config/local.env`；
7. 把 `characters/ramlethal.eidolon` 导入到 `config/characters/ramlethal/`。

如果提示 `Permission denied`：

```bash
chmod +x scripts/*.sh
./scripts/setup.sh
```

## 6. 填写 API key

编辑：

```bash
open -e config/local.env
```

至少填这三项：

```text
CHAT_LLM_API_KEY=你的 DeepSeek key
DASHSCOPE_API_KEY=你的阿里云百炼 key
FISH_AUDIO_API_KEY=你的 Fish Audio key
```

对应控制台：

- DeepSeek：<https://platform.deepseek.com/>
- 阿里云百炼 / DashScope：<https://bailian.console.aliyun.com/>
- Fish Audio：<https://fish.audio/>

如果你已经有一个 `.env` 文件，可以导入：

```bash
./scripts/import_keys.sh /path/to/your.env
```

它只复制已知的 key，不打印值，并把目标权限设为 600。

检查文件权限：

```bash
ls -l config/local.env
# 应该是 -rw-------
```

**不要把 `config/local.env` 上传、截图或发给别人。**

## 7. 启动

```bash
./scripts/run.sh
```

第一次启动时：

1. macOS 可能询问麦克风权限。选择“允许”。  
   如果没有弹窗，或误点了拒绝：  
   **系统设置 → 隐私与安全性 → 麦克风**，允许 `Eidolon Assistant` / `Electron`。
2. 应用窗口出现。先发一句文字：“你好”。
3. 如果角色能回复，说明 LLM 正常；如果只显示文字但没声音，检查 Fish Audio key 和音色。
4. 按“开始听”测试麦克风；在 **设置 → 麦克风与唤醒** 里看实时电平。

## 8. 导入第二个角色

默认已经导入拉姆蕾萨尔。要使用二阶堂希罗：

1. 打开 **设置 → 角色**；
2. 点 **导入 .eidolon**；
3. 选择：

```text
characters/hiro.eidolon
```

4. 在角色列表里选中希罗 → **切换到此角色**。

仓库里只保留可导入的 `.eidolon`；`config/characters/` 下的展开目录是运行时生成的，
不进入 git。

## 9. 日常运行方式

```bash
./scripts/run.sh                  # 构建并启动 Electron 桌面应用
./scripts/dev.sh                  # 开发模式（热更新）
./scripts/backend.sh --port 8799  # 只启动 backend，浏览器打开 http://127.0.0.1:8799/
./scripts/check.sh                # 检查依赖/模型/角色/配置是否齐全
./scripts/test.sh                 # 跑测试和构建
```

只用浏览器模式时，macOS 的麦克风权限会算在 **Terminal** 上：  
系统设置 → 隐私与安全性 → 麦克风 → 允许 Terminal。

## 手动安装（不用 setup.sh）

如果 `setup.sh` 失败，可以自己执行等价步骤：

```bash
# 1. Python
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e backend

# 2. Node / desktop
cd desktop
npm ci
npm run build
cd ..

# 3. KWS 模型
./scripts/fetch_models.sh

# 4. 配置
cp config/local.env.example config/local.env
chmod 600 config/local.env
# 编辑 config/local.env 填入 key

# 5. 导入默认角色
.venv/bin/python - <<'PY'
from pathlib import Path
from eidolon_assistant.characters import import_archive
from eidolon_assistant.config import CONFIG_DIR

import_archive(CONFIG_DIR, Path("characters/ramlethal.eidolon").read_bytes())
print("imported ramlethal")
PY

# 6. 检查并启动
./scripts/check.sh
./scripts/run.sh
```

## 故障排查

### `python3: command not found` / 版本太低

安装 Python 3.12+，重新打开 Terminal。不要用 macOS 自带的旧 Python 代替。

### `node: command not found` / 版本太低

安装 Node.js 20+；确认 `node -v` 和 `npm -v`。

### `check.sh` 报缺东西

按提示重新执行：

```bash
./scripts/setup.sh
```

或单独补：

```bash
./scripts/fetch_models.sh
```

### Electron 启动后提示“后端没有启动”

在项目根目录运行：

```bash
.venv/bin/python -m eidolon_assistant serve --port 8799
```

看终端里的错误。常见原因：

- 没安装 backend 依赖 → `./scripts/setup.sh`
- 没填 key → 编辑 `config/local.env`
- 没有默认角色 → 重新运行 `setup.sh`
- 没有 KWS 模型 → `./scripts/fetch_models.sh`

### 麦克风没有电平

- 系统设置 → 隐私与安全性 → 麦克风；
- Electron 模式允许 `Eidolon Assistant` / `Electron`；
- 浏览器/`backend.sh` 模式允许 `Terminal`；
- 在设置 → 麦克风与唤醒里确认输入设备。

### Harness 任务失败

```bash
ls "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
```

确认路径与 `config/settings.toml` 的 `[harness] command` 一致，并且 Harness 已登录。

### 端口被占用

```bash
./scripts/backend.sh --port 8799
```

### DSH 环境里 Electron 被当成 Node 启动

```bash
env -u ELECTRON_RUN_AS_NODE ./scripts/run.sh
```

## 卸载 / 重置

- 删除项目目录即可删除源码和 `.venv`；
- 运行数据在：

```text
~/Library/Application Support/EidolonAssistant/
```

删除这个目录会清除配置、密钥、日志、SQLite、工作目录和已导入角色。  
**删除前请确认里面没有你想保留的数据。**

## 仍需注意

- 本项目没有签名/公证的 `.app`；首次启动可能被 Gatekeeper 拦截（如果是通过浏览器下载的
  zip）。右键 → 打开，或在终端执行 `xattr -dr com.apple.quarantine <目录>`。
- 角色头像和角色人设来自原作品；公开分发前请确认版权。
- 默认角色的 Fish Audio `voice_id` 来自原项目；分享角色包时建议换成你自己的音色 id。
