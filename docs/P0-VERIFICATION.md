# P0 技术验证报告

日期：2026-10-25
环境：macOS 27.0 (arm64)，DeepSeek Harness 0.2.1-rc.2，Python 3.13.2，Node 24.13.0
项目目录：`eidolon-assistant/`

结论：**P0-A 通过；P0-B 通过（本地唤醒 + 真人唤醒均验证）；P0-C 的语音闭环通过（真人说唤醒词 + 命令，完整走通 Harness 任务并用角色语音回复）。**

---

## P0-A：ACP 验证（通过）

### 启动方式

不需要 npm 安装，也不需要修改应用包。应用包内自带可执行 launcher：

```text
/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh
  --profile acp
```

`dsh` launcher 自己设置 `ELECTRON_RUN_AS_NODE=1` 并执行 app.asar 内的
`@deepseek-ai/dsh-desktop-host/lib/cli.js`。

注意：本机桌面 DSH 会向子进程注入 `DSH_PROFILE=desktop`、`DSH_PROFILE_DIR`、
`DSH_WEB_URL`、`DSH_SESSION_ID`。ACP 子进程必须清掉这些变量，只保留
`DSH_HOME=~/.dsh`，否则会去引导 desktop profile。

### 已验证的协议行为

| 步骤 | 结果 |
|---|---|
| `dsh --version` | `0.2.1-rc.2` |
| `dsh --profile acp --help` | 正常打印帮助并退出，不占用 stdin/stdout |
| `initialize` | `protocolVersion: 1`，agentInfo `deepseek-harness-acp/0.0.1` |
| capabilities | `sessionCapabilities: {close, list, resume}`、`mcp.http: true`、`promptCapabilities.image:false/audio:false` |
| `session/new`（绝对 cwd） | 返回 sessionId 和 `model` 配置项（deepseek-official / deepseek-v4-flash / v4-pro 等） |
| `session/prompt("列出当前目录")` | 3.06s，`stopReason: end_turn`，收到 `tool_call`（bash）与 `agent_message_chunk` |
| `session/close` | 返回 `{}`，进程 EOF 后正常退出 |
| 进程退出 | exit code 0，无孤儿进程 |

`session/update` 里可用的信息（直接决定 UI 能显示什么）：

- `agent_message_chunk`（助手正文）与 `agent_thought_chunk`（思考）；
- `tool_call`：`toolCallId`、`title`（bash / write / web_search…）、`status`、`rawInput`
  （`command`、`path`、`sandbox_permissions`、`justification`）；
- `tool_call_update`：状态变化与工具输出文本；
- `usage_update`。

### 权限语义（关键发现）

DeepSeek Harness 的审批不是“每次工具调用都问”，而是 **当操作需要突破当前沙箱时才问**。
`DSH_PERMISSION_MODE` 决定沙箱模式和审批策略：

| `DSH_PERMISSION_MODE` | 实际行为 |
|---|---|
| `workspace-write`（默认） | 工作区内写文件、执行 `ls`/`sleep 120` 都不询问；**不会触发 approval** |
| `read-only` | 写文件、删除、需要写权限的 shell 先失败，模型带 `sandbox_permissions` 重试，此时 ACP 发 `session/request_permission` |
| `danger-full-access` | 审批策略为 `never`，所有操作直接执行 |

P0 实测（`read-only`）：

- 请求 `session/request_permission` 时 `toolCall` 只带 `toolCallId`，不带标题/命令；
  **必须先用之前的 `tool_call.rawInput` 建索引，才能展示“它想做什么”**。
- 选项固定为两个：`allow-once`（Allow once）、`reject-once`（Reject）。
- 拒绝后：工具结果返回 “the user rejected escalating…”，模型停止并解释；文件没有被创建。
- 允许后：同一命令重试成功，文件写入内容正确。

因此本项目的默认权限策略采用：

```text
DSH_PERMISSION_MODE=read-only   → 默认（高风险操作必须确认）
DSH_PERMISSION_MODE=danger-full-access → UI 里显式开启“全部自动批准”
```

副作用：在 `read-only` 下，被拒绝的写操作会先失败一次再重试，多一轮模型调用（约 1–2 秒）。

### 取消

- `session/cancel` 之后 0.2s 内 `session/prompt` 返回 `stopReason: cancelled`；
- 后台 `sleep 120` 子进程也被一并终止；
- 若 8s 内不收敛，Bridge 会 kill 整个进程组（`setsid` 启动），并允许重新拉起。

### 进程与日志

- ACP 的 stdout 全程只有 JSON-RPC 帧；P0 期间 stderr 为空；
- Bridge 用 `start_new_session=True`，退出时先 `session/close`、关 stdin，再按需
  SIGTERM/SIGKILL 进程组；实测无残留 `dsh` 进程。

### 网络相关（与 ACP 无关，但阻断 TTS）

- 本机 `api.fish.audio` 的 DNS 被污染（解析到 `199.59.150.43`），直连超时；
- 走 macOS 系统代理 `127.0.0.1:7892` 时正常（HTTP 200，返回 mp3）；
- DashScope WebSocket 必须**关闭**代理（Python `urllib.getproxies()` 在 macOS 会读取系统 SOCKS
  设置，导致 websockets 试图走 SOCKS）；
- 已实现 TTS 的“代理 → 直连”双路径重试，任一成功即用。

---

## P0-B：本地唤醒词（通过）

### 方案

- 引擎：`sherpa-onnx` 2.0.0；
- 模型：`sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`
  （32.6MB，zipformer，建模单元为拼音声母/韵母，支持自定义中文关键词）；
- 关键词：`拉姆` → `l ā m ǔ`，`拉姆蕾萨尔` → `l ā m ǔ l ěi s à ěr`；
- 关键词由角色配置的 `wake_words` 经 `pypinyin` 自动转拼音并校验模型词表生成；
- 完全本地，不调用云端 STT。

模型下载：GitHub Releases 直连很慢（约 4KB/s），使用 `gh-proxy.com` 镜像完成。

### 合成语音矩阵（`say -v Tingting/Meijia/Sinji` 生成）

| 用例 | 结果 |
|---|---|
| 拉姆（Tingting） | ✅ 0.8s 检出 |
| 拉姆（Meijia） | ✅ 0.8s 检出 |
| 拉姆（慢速 150wpm） | ✅ 0.8s 检出 |
| 拉姆（Sinji 粤语语音） | ❌ 未检出（发音体系不同） |
| 拉姆蕾萨尔 | ✅ 被识别为“拉姆” |
| 拉面 | ✅ 无误报 |
| 你好 / 今天天气不错 / 帮我查一下明天北京的天气 | ✅ 无误报 |

延迟：0.66s 的“拉姆”在音频第 0.8s 检出（含 0.1s 分块 + 尾部静音提交），约 140ms 量级。

### 资源占用（16kHz 单声道，100ms 分块）

| 配置 | CPU（单核百分比） |
|---|---|
| 仅麦克风采集 | 0.6% |
| KWS，1 线程 | 4.3% |
| KWS，2 线程 | 19.9% |

结论：**1 线程**（已设为默认）。常驻内存约 50MB。

### 真人唤醒

测试中用户对 MacBook 麦克风说「拉姆」，2 秒内 UI 进入 `speaking`（播放「……ここにいる。」），
随后自动进入 `listening` 并完成录音。唤醒检测成功。

### 回声策略

- TTS 播放全程挂起 KWS（`suspend_wake`），播放结束再等 600ms 才恢复；
- 恢复时 reset 解码流，避免残留状态；
- 实测摘要 TTS 播放 11 秒期间没有二次唤醒。

---

## P0-C：语音闭环（通过）

真实时间线（用户真声，系统音量 3/100）：

| 时间 | 事件 |
|---|---|
| 0.0s | 唤醒监听中 |
| 29.9s | `wake 拉姆` |
| 29.9–32.9s | 播放唤醒回应「……ここにいる。」（TTS 缓存命中） |
| 32.9s | `listening`（录音） |
| 35.2s | VAD 检测到静音，进入 `transcribing` |
| 37.7s | STT 结果：`今天的北京天气。` |
| 38.8s | 意图：`task` |
| 39.8s | 标准化：`查询今天北京的天气情况` |
| 39.8–51.2s | Harness 执行（web_search + web_fetch），11.4s |
| 52.3s | 角色总结（日语 + 中文字幕） |
| 54.6–65.8s | TTS 播放总结 |
| 65.8s | 回到 `idle` |

验证点：

- macOS 麦克风权限：由 DeepSeek Harness 派生的进程可直接采集，无额外弹窗；
- 音频输出正常（`afplay` 播放 Fish Audio mp3）；
- 录音阶段 KWS 是挂起的，录到的不是自己的 TTS；
- 失败兜底：无语音输入 → “没有听到命令。”并回到 idle；STT 失败 → 提示并回到 idle。

### 观察到的问题

1. **STT 把日期听成了“今天”**（用户原话待确认；若说的是“明天”则是识别错误）。
   中文日期是任务型语音的高风险点，需要在命令标准化层做二次确认或让角色复述。
2. 总结 TTS 播放约 11s，偏长；后续可以按句切分、边说边生成，或改用更快的 TTS 模型。
3. 唤醒后到开始录音之间固定有约 3s（TTS + 600ms 冷却），用户需要等回应结束再说话。
4. 系统音量 3/100，语音几乎不可闻；UI 字幕弥补了这一点。

---

## 与设计文档的差异 / 决定

- 使用应用包内的 `dsh` launcher，而不是 npm 安装，避免修改安装包、避免重复下载运行时。
- 复用 `~/.dsh` 的登录凭据（不复制、不读取密钥），因此 ACP 会话与桌面版共享历史存储。
- 权限模式用 DSH 自己的沙箱语义（read-only / danger-full-access），不另造一套工具白名单。
- TTS 依赖系统代理；已做自动探测与直连回退。
- 唤醒词模型放在项目 `models/`，不放入仓库（`.gitignore`）。

## 下一步

P1–P3 已按设计实现（Electron 悬浮窗、FastAPI/WebSocket、KWS/STT/TTS、ACP Bridge、
权限确认、取消、任务记录、原始面板）。剩余：P4 的打包与更细的验收脚本。
