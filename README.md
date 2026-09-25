# CherryVoice

一个 **Windows 桌面流式语音输入法**：按住热键说话，屏幕上**实时看着字长出来**；说完后由大模型**整段修正**，最后把干净的文本**一次性注入当前输入框**。

> 开发状态：**P1 完成**（WSL 流式识别服务已跑通并实测）。P2~P5 进行中，见 [开发计划](#开发计划)。

## 它和别的语音输入有什么不一样

| 能力 | 现有开源工具 | CherryVoice |
|---|---|---|
| 流式显示（边说边出字） | 极少，且多为 macOS 独占 | ✅ |
| AI 修正（说完整段重写） | 多数有 | ✅ |
| **流式翻译**（原文/译文同时滚动） | 几乎没有 | ✅ |
| 接入**自研/私有 ASR** | 几乎都写死服务商 | ✅ 对接 Confucius4-R2T2 |
| Windows | 支持少 | ✅ Windows 10/11 |

## 架构：为什么必须两段式

```
┌──────────── Windows 客户端（本项目主体）────────────┐
│  全局热键 ─→ 麦克风采集 ─→ WebSocket 发 16k PCM 块    │
│                              ↓                      │
│  悬浮窗 ←── 增量文本 ─────────┘                      │
│  松开热键 → LLM 修正/翻译 → 注入当前输入框            │
└───────────────────────┬─────────────────────────────┘
                        │ ws://127.0.0.1:18300
┌───────────────────────▼─────────────────────────────┐
│  WSL 识别服务：R2T2LlamaASRModel.LlamaNative + CUDA  │
└─────────────────────────────────────────────────────┘
```

* 识别模型跑在 WSL（Linux + CUDA），Windows 客户端无法直接调用；
* 麦克风、全局热键、不抢焦点悬浮窗、文本注入，WSL 又做不到；
* WSL2 支持 **localhost 转发**：WSL 内监听 `0.0.0.0:18300`，Windows 直接访问 `127.0.0.1:18300`（已实测，握手 ~17 ms）。

## 快速开始

### 0. 前置条件

* Windows 10/11 + WSL2（Ubuntu），识别模型已按 `docs/` 的说明部署在 WSL 的专用账号下
* 一个 OpenAI 兼容的 LLM 接口（用于修正/翻译；**不可用时自动降级为注入原文**）

### 1. 部署 WSL 流式识别服务

```bash
# 在 Windows 上执行（只新增 /home/<user>/cherryvoice，不改动任何现有文件）
wsl.exe -d Ubuntu -u r2t2 -- bash -l "/mnt/d/<仓库路径>/scripts/deploy_wsl.sh" --install-service --restart
```

服务默认监听 `0.0.0.0:18300`，日志在 `/home/r2t2/cherryvoice/logs/`。

### 2. 验收 / 自测

```bash
# 零依赖客户端，Windows 与 WSL 都能跑
python tools/test_stream_client.py --wav some16k.wav --url ws://127.0.0.1:18300
```

也可让服务端脱离网络自检：

```bash
python wsl/r2t2_stream_server.py --selftest-wav /path/to/test.wav
```

## 目录结构

```
├── wsl/          WSL 流式识别服务（WebSocket）
├── client/       Windows 客户端（热键 / 采集 / 悬浮窗 / 识别客户端；P3 起含注入与 LLM）
├── tools/        诊断与验收工具（零第三方依赖，见「自测」）
├── scripts/      部署脚本与 systemd 用户服务
├── docs/         架构说明、协议、开发指南、竞品调研
└── resources/    测试音频等（不提交模型权重）
```

## 两个踩过的 Windows 坑

1. **`WS_EX_NOACTIVATE` 必须加在顶层窗口上**。tkinter 的 `winfo_id()` 返回的是
   `TkChild` 子窗口，真正会被激活的是它的父级 `TkTopLevel`。加错了地方等于没加，
   悬浮窗照样抢焦点、照样把文本注入到错误的窗口。`client/win32.py` 的
   `top_level_window()` 负责这件事。
2. **DPI 感知要在建窗前设**。否则缩放显示器上既模糊、坐标又是错的。

## 协议

见 [docs/protocol.md](docs/protocol.md)（与需求文档 §7.1 一致）。要点：

* 客户端 → 服务端：二进制帧（16 kHz / 单声道 / int16 LE PCM，建议 160 ms）、
  `{"type":"start",...}`、`{"type":"finish"}`、`{"type":"cancel"}`
* 服务端 → 客户端：`{"type":"partial","text":"累积全文"}`、
  `{"type":"final","text":"..."}`、`{"type":"error","message":"..."}`

`partial` 是**累积全文**（不是增量），客户端直接替换显示即可，后端偶发回滚天然被覆盖。

## 开发计划

| 阶段 | 交付物 | 状态 |
|---|---|---|
| P1 | WSL 流式 WebSocket 识别服务 | ✅ 已完成并双侧实测 |
| P2 | Windows 客户端骨架：热键 + 麦克风 + 不抢焦点悬浮窗 | ✅ 已完成（机械可验部分全过；真人按键+说话验收待做） |
| P3 | LLM 修正 + 文本注入（完整闭环） | 🟡 代码完成，自测通过；真机 LLM 连通待 API Key |
| P4 | 流式双语翻译 | 🟡 代码完成，自测 15/15 + 集成 5/5；真机 LLM 连通待 API Key |
| P5 | 设置界面 + 打包 + 文档 + 发布 | 🟡 代码与文档全部完成；**仅差 GitHub 发布（待仓库名）** |

### 流式翻译怎么做的（竞品都没有现成实现）

原文按**句子边界**切成「已落定段」和「开口段」。已落定段**永不重译** ——
只有遇到句末标点、静默 ~1.5 秒、或缓冲超过 N 字，才把新内容送去翻译，
译文因此是**纯追加**的，不会边翻边抖。后端若回退了已落定的文字，
就丢掉受影响的尾部段重译，而不是硬拼。

> 注意：**逗号不算句子边界**。按逗号切会把语义切碎，译文读起来很跳。

## 自测（都不需要人按键 / 不需要人说话）

```bash
# 一键跑完所有离线自测（推荐；CI 也是跑这个）
python tools\run_all_tests.py
python tools\run_all_tests.py --fast          # 跳过较慢的界面/回放类
python tools\run_all_tests.py --only llm      # 只跑名字含关键词的

# 也可以单独跑：
python tools\test_win32_signatures.py         # Win32 ctypes 签名审计（防 64 位截断）
python tools\test_overlay_focus.py --shot overlay.png   # 悬浮窗不抢焦点（含阳性对照）
python tools\test_mic_path.py --wav a.wav     # 真实音频链路（虚拟声卡回环）
python tools\test_hotkey.py                   # 热键：轮询逻辑 + 钩子状态机
python tools\test_injection.py                # 注入：剪贴板规则 + Ctrl+V / 键入
python tools\test_llm.py                      # LLM 失败分类与输出校验（假服务）
python tools\test_translator.py               # 增量翻译：三种触发 + 只翻新增
python tools\test_translate_flow.py a.wav     # 翻译接到主流程
python tools\test_closed_loop.py a.wav        # 完整闭环（假 LLM）
python tools\test_settings.py                 # 设置界面配置往返（含截图）
python tools\test_stream_client.py --wav a.wav  # 识别服务验收（零依赖）
python tools\probe_key_injection.py           # 探测合成按键能否驱动热键
python tools\check_release.py                 # 发布前安全检查
```

> `tools\check_release.py` 退出码非 0 就**不许发布**。它自己也被验证过：
> 仓库干净时报"可以发布"，故意塞一个假 Key 进去会被抓成 BLOCKER。
> 若某一行确实需要豁免（比如测试里的假数据），在该行加注释
> `release-check: allow` 即可。

正常使用：`python -m client.app`（按住热键说话，松开后出结果）。
设置界面：`python -m client.app --settings`。

## 打包成免安装 exe

```bash
.venv\Scripts\python.exe scripts\build_exe.py              # 目录版（默认，启动快）
.venv\Scripts\python.exe scripts\build_exe.py --onefile    # 单文件版
.venv\Scripts\python.exe scripts\build_exe.py --console    # 保留控制台，排查用
```

产物在 `build/dist/CherryVoice/`（约 58 MB，**自带 Python 与 PortAudio，目标机器不需要装任何东西**）。
推 tag（`v*`）时 GitHub Actions 会自动跑同样的流程并把产物传成 artifact，见
`.github/workflows/build.yml`。

> 打包版**没有控制台窗口**（它是输入法，不该常驻黑框）。所以运行日志同时写在
> `%APPDATA%\CherryVoice\logs\client.log`，出问题看那个文件。

## 依赖的外部服务与许可

* **ASR 模型**：Confucius4-R2T2（网易有道）——代码 Apache-2.0，**权重按 NetEase Model Use License**（与代码许可不同，商用请注意）。权重不随本仓库分发。
* 本项目**不安装真 vLLM**：仓库的 `r2t2/r2t2_asr.py` 有 import 顺序 bug，已在 venv 中放置替身（见需求文档 §6.2②）。
* License：见 [LICENSE](LICENSE)。

## 已实测的性能基线（本机 RTX 4070 Laptop / WSL2）

| 项 | 实测值 |
|---|---|
| 模型加载 | 4.3 s（首次冷启动 8.3 s） |
| 6.74 s 音频流式解码 | ~3.8 s（0.56× 实时），首字 **1.20 s** |
| 20.22 s 音频流式解码 | 14.5 s（0.72× 实时） |
| 33.70 s 音频流式解码 | 29.6 s（0.88× 实时） |
| **53.92 s 音频流式解码** | **62.2 s（1.15× 实时 —— 跟不上说话了）** |
| 分段后（每段 8 s，同一份 33.7 s 音频） | **0.54× 实时，单块最慢从 288 ms 降到 162 ms** |
| Windows → WSL 握手 | 17 ms |
| 显存占用 | ~2.5 GB |

> 长语音会明显变慢（流式解码每块都要重编码累积音频）。客户端默认在
> **连续说话 30 秒**时自动分段重建会话，对用户无感。详见
> [docs/protocol.md](docs/protocol.md)。

### 长时间运行（需求文档 风险 #7）

连续跑 20 轮识别（每轮 6.74 s 音频）实测：

| 指标 | 表现 |
|---|---|
| 服务进程 RSS | **2010 MB，20 轮完全一致（无增长）** |
| 显存 | 7836 MiB，恒定 |
| 解码/实时 | 0.52–0.64，**无上升趋势**（首轮 0.64，末轮 0.59） |

即：**"长时间运行后变慢"在本机 20 轮内没有复现** —— 内存与显存都不涨，延迟只是正常抖动（±10%）。
不过这只是 20 轮的观测，不是证明；真要长期挂着用，建议偶尔看一眼 `logs/service.log` 里的
`decode_ms` 有没有系统性抬高。

### 服务崩了会怎样（需求文档 风险 #2 / #6）

实测 `kill -9` 掉识别服务：

```
r2t2-stream.service: Main process exited, code=killed, status=9/KILL
r2t2-stream.service: Scheduled restart job, restart counter is at 2.
r2t2-stream.service: Started CherryVoice R2T2 streaming ASR service
★ 从进程消失到端口重新可用：13.4 秒
```

即 systemd 的 `Restart=always` 确实兜住了（约 13 秒 = `RestartSec=5` + 模型重新加载）。
客户端侧会自动重连（已用 `tools/test_asr_reconnect.py` 单独验证过），
所以用户感受到的就是"十几秒后又能用了"，不会永久失效。

### 识别稳定性

同一段音频重复跑 10 次，最终文本（含标点）**10/10 完全一致**。
不过注意：标点并非在所有路径下都一样 —— 走麦克风回环时同一句话曾出现
`酒水，也没` 与 `酒水也没` 的差异，所以需求文档 风险 #5 说的"标点抖动"
在**换音频路径**时仍然存在，只是同一路径下是稳定的。

### 兼容性验收（需求文档 §10.3：至少 5 个应用）

用 `tools/test_inject_apps.py` 往**真实第三方应用**注入并读回验证
（注入后 `Ctrl+A`/`Ctrl+C`，从剪贴板读回比对）：

| 应用 | 结果 | 说明 |
|---|---|---|
| 记事本 | ✅ 通过 | Win11 Store 版，剪贴板+Ctrl+V，读回一致；标题变为 `*CherryVoice…` 佐证文档已改 |
| Word | ✅ 通过 | `文档1 - Word`[OpusApp]，**逐字键入**模式，读回一致 |
| VS Code | ⚪ 本机未安装 | —— |
| 微信 / 飞书 | ⚪ 本机未安装 / 启动无窗口 | —— |

Word 那次之所以走"逐字键入"而不是剪贴板，是因为剪贴板里当时有 Word 复制出的
**富文本（含非文本格式）**，非文本保护逻辑自动改走了键入 —— 顺带把这条兜底路径
也在真实 Office 应用里验证了。

> 本机只装了记事本和 Word，另外三个装不了，所以 §10.3 的"5 个应用"
> **只完成了 2 个**。这两个是实打实注入进去并读回来的，不是"应该没问题"。
