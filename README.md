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
# 1. 悬浮窗不抢焦点 —— 需求文档列为「最该先验证的」
#    带阳性对照：会另外开一个普通窗口，确认检测器真的能发现抢焦点行为
python tools\test_overlay_focus.py --shot overlay.png

# 2. 真实音频采集链路（走本机 VB-Audio 虚拟声卡回环，无需对着麦克风说话）
python tools\test_mic_path.py --wav some16k.wav

# 3. 全局热键：确定性逻辑验证 + 真实按键注入探测
python tools\test_hotkey.py

# 4. 文本注入：剪贴板规则 + Ctrl+V 注入 + 逐字键入
python tools\test_injection.py

# 5. LLM 修正/翻译的失败分类与输出校验（本地假服务，不需要 API Key）
python tools\test_llm.py

# 6. 完整闭环：识别 → LLM 修正 → 注入前台输入框（本地假 LLM）
python tools\test_closed_loop.py some16k.wav

# 7. 流式翻译：三种触发条件 + 只翻新增部分（假 LLM，无需 Key）
python tools\test_translator.py

# 8. 流式翻译接到主流程（开启=提交译文 / 关闭=提交原文 / 失败=回退原文）
python tools\test_translate_flow.py some16k.wav

# 9. 端到端：回放一份 WAV，走完整的「热键回调 → 采集 → 识别 → 悬浮窗」链路
python -m client.app --replay some16k.wav --exit-after-final

# 10. 只跑识别+决策、不往任何窗口灌字
python -m client.app --replay some16k.wav --exit-after-final --inject-mode none --no-llm

# 11. 开启流式翻译（提交译文而不是原文）
python -m client.app --translate --target-language English

# 12. 打开设置界面（FR-7 六个分组）
python -m client.app --settings

# 13. 设置界面配置往返自测（含截图）
python tools\test_settings.py

# 14. **发布前安全检查**：扫 API Key / 个人路径 / 模型权重 / .gitignore 覆盖
python tools\check_release.py

# 15. 列出可用的输入设备
python -m client.app --list-devices
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
