# EtherealFlow · 缥缈心流

**真流式语音输入法** · 本地模型 · 语音不出你的机器

> 按住热键说话，屏幕上**实时看着字长出来**；松开后把干净的文本注入当前输入框。
> 识别模型跑在**你自己的机器上**，语音不经过任何云端。

![真流式输入效果](docs/assets/streaming-demo.gif)

`Windows 10/11` · `WSL2` · `Python` · `MIT`

---

# 中文

## 一、为什么叫「真流式」

市面上多数语音输入是**说完才出字**：你要按住说完整段、松开、再等一两秒，屏幕才一次性跳出结果。
说错了只能重来 —— 因为你看不到过程。

EtherealFlow 是**边说边出**：

| | 传统语音输入 | EtherealFlow |
|---|---|---|
| 出字时机 | 松手后一次性出现 | **说话过程中逐字增长** |
| 能否中途察觉说错 | ❌ | ✅ 看到字就知道它在听、听对没有 |
| 后端协议 | 整段请求 | **累积文本流**（`partial` 持续推送） |
| 松手后 | 直接注入 | 修正/翻译后再注入 |

它的识别后端（Confucius4-R2T2）本身是 **append-only 的真流式模型**：已输出的文字永不回改，
所以悬浮窗里看到的字是稳定增长的，不会来回跳。这正是"**真**流式"与"分段伪流式"的区别。

### 名字的来历

**EtherealFlow = 缥缈心流**。

中文的「**流**」一个字同时装了两层意思：

- 「心**流**」—— 心理学上的 **flow**，沉浸忘我、思路不断的状态
- 「**流**式」—— 边说边出字的技术特征

而 `Ethereal`（缥缈）= 缥缈心。

## 二、本地模型：语音不出你的机器

这是本项目与绝大多数语音输入工具最根本的区别。

```
┌──────────── 你的 Windows 电脑 ────────────┐
│                                           │
│   麦克风 → 客户端 ──┐                      │
│                    │ ws://127.0.0.1:18300 │
│   悬浮窗 ←─────────┘                      │
│                    ↓                      │
│         WSL2 里的识别服务                  │
│         Confucius4-R2T2（llama.cpp + CUDA）│
│         模型权重常驻显存，约 2.5 GB          │
│                                           │
└───────────────────────────────────────────┘
        全程不联网 · 不经过任何云端服务
```

- **语音数据不出本机**：音频只发到 `127.0.0.1:18300`，即你这台机器上的 WSL 实例。
- **识别不需要 API Key、不需要按量计费**：模型权重在本地，说多少句都一样。
- **可换成你自己的 ASR**：客户端只认一个 WebSocket 协议（见 [docs/protocol.md](docs/protocol.md)），
  想接自研/私有的识别引擎，改服务端即可。
- 大模型修正/翻译是**可选**的（默认关闭）。它同样可以跑在**你自己的机器上**
  （Ollama / LM Studio / llama.cpp server / vLLM），也可以接任意 OpenAI 兼容的云端接口 ——
  见 [五之 5. 接一个大模型](#5-接一个大模型修正与翻译)。

> ⚠️ **模型权重需自行获取**：本仓库不分发权重（GB 级）。
> 识别引擎为 [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2)（网易有道），
> 代码 Apache-2.0、**权重按 NetEase Model Use License**（与代码许可不同，商用请自行确认）。
> 部署步骤见 [docs/development.md](docs/development.md)。

## 三、功能

| 功能 | 状态 |
|---|---|
| 全局热键按住说话 / 松开结束 / Esc 取消 | ✅ |
| 16 kHz 单声道采集（设备不支持时自动重采样） | ✅ |
| **流式识别显示**（逐字增长、不抢焦点悬浮窗） | ✅ |
| 文本注入（剪贴板+Ctrl+V 主方案 / 逐字键入兜底 / 剪贴板保护） | ✅ |
| 长语音自动分段（避免长音频解码跟不上实时） | ✅ |
| 断线自动重连 / 服务崩溃自动恢复 | ✅ |
| 设置界面（热键 / 识别 / 修正 / 翻译 / 注入 / 词典） | ✅ |
| **LLM 整段修正**（去口水词 / 补标点 / 纠同音错别字 / 数字规范化） | ✅ 已接真实模型实测 |
| **三种修正力度**（不整理 / 轻度整理 / 深度整理，卡片式选择） | ✅ |
| **自定义修正模式**（自己起名、写说明与 Prompt，与内置模式同等对待） | ✅ |
| 去除结尾句号（接着说下一句时不被硬塞句号） | ✅ |
| **流式双语翻译**（原文/译文并行、只翻译新增部分、已落定段永不重译） | ✅ 已接真实模型实测 |
| 任意 OpenAI 兼容接口（本机 Ollama / LM Studio / llama.cpp / vLLM / 云端） | ✅ 含预设、模型列表、连接自检 |

## 四、实测性能（RTX 4070 Laptop / WSL2）

| 项 | 实测 |
|---|---|
| 模型加载 | 4.3 s |
| 6.74 s 语音流式解码 | 0.56× 实时，首字 **1.20 s** |
| 33.70 s 语音流式解码 | 0.88× 实时 |
| 53.92 s 语音流式解码 | **1.15× 实时（跟不上）** → 故客户端 30 秒自动分段 |
| 分段后（每段 8 s，同一份 33.7 s 音频） | **0.54× 实时** |
| Windows → WSL 握手 | 17 ms |
| 打包体积 | 58 MB（自带 Python，免安装） |

> 首字 ~1.2 s 里约 1.1 s 是**模型本身需要攒够音频才吐第一个字**，不是管线开销。
> 长语音会明显变慢（流式解码每块都要重编码累积音频），所以客户端默认连续说 30 秒就自动分段重建会话，对用户无感。

## 五、快速开始

### 1. 部署 WSL 识别服务

```bash
# 在 Windows 上执行（只新增 ~/etherealflow，不改动任何现有文件）
wsl.exe -d Ubuntu -u <用户> -- bash -l "/mnt/d/<仓库路径>/scripts/deploy_wsl.sh" --install-service --restart
```

### 2. 运行客户端

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install numpy sounddevice websockets
.venv\Scripts\python.exe -m client.app            # 或双击「启动 EtherealFlow.bat」
```

### 3. 用法

在任意输入框里，**按住 `Ctrl + Win`** 说话，**松开**即输入。
录音中按 `Esc` 取消本次输入。

> **第一次用没反应？** 检查麦克风选择 —— 配置里 `audio.device` 需指向**真实麦克风**
> （很多机器默认录音设备是虚拟声卡）。用 `--list-devices` 看编号，再到设置界面里选。

设置界面：`python -m client.app --settings`

### 4. 自测

```bash
python tools\run_all_tests.py        # 一键跑完 11 个测试
python tools\check_release.py        # 发布前安全检查
```

其中绝大多数**不需要人按键、不需要说话、不需要 API Key**。
需要真实环境的几项会自己报 `[SKIP]` 并说明原因，不会伪装成通过。

### 5. 接一个大模型（修正与翻译）

**可选**，默认关闭。开启后松手的那一步会多一次 LLM 调用：按你选的**修正模式**
处理，或按你的设置直接**翻译**后提交译文。

#### 修正模式：同一句话，三种力度

「修正」不该只有一种力度。只想改错别字的人，被模型重写一遍会很恼火；
而要发出去的文字，又确实需要整理。所以内置三种，**点卡片选择**：

| 模式 | 做法 | 同一句的效果 |
|---|---|---|
| **不整理** | 只修明显的识别错误，其余一字不动 | 嗯我跟你说明天**会议**吧不对不对先讨论排期再聊产品方案 |
| **轻度整理** | 去口水词与自我修正、补标点，**保留措辞** | 我跟你说，先讨论排期，再聊产品方案。 |
| **深度整理** | 保留原意，整理成书面表达 | 我跟你说，先讨论排期，再聊产品方案。 |

（原文：`嗯我跟你说明天回议吧不对不对先讨论排期再聊产品方案`）

另外有个**去除结尾句号**的开关：很多人说话是接着上一句说的，系统硬补一个句号反而碍事。

**也可以自己加模式**：点「新增模式」，填名称、一句话说明、示例和 Prompt
（用 `{text}` 表示识别原文）。自定义模式与内置模式在界面上完全同等对待。
想改成你习惯的写法，不必去动内置的三个 —— 它们是产品的默认行为。

#### 接入模型

打开设置界面 → **修正**页 → 先用「模型接入」选一个预设，再点「测试连接」：

| 场景 | 预设 | 说明 |
|---|---|---|
| **完全本地**（推荐） | 本地 · Ollama | 需先 `ollama serve` 并 `ollama pull qwen2.5:7b` |
| **完全本地** | 本地 · LM Studio | 在软件里加载模型并启动本地服务器 |
| **完全本地** | 本地 · llama.cpp server / vLLM | 填它们监听的地址即可 |
| 云 端 | OpenAI / DeepSeek / Kimi / 智谱 / 通义 / 硅基流动 | 选预设后只需要再粘一个 API Key |

几个实测出来的要点：

- **本机服务不需要 API Key**，留空即可；填了反而可能被某些实现拒掉。
  客户端在不填 Key 时**不会**发送 `Authorization` 头。
- **地址怎么填都行**：`http://localhost:11434`、`http://localhost:1234/v1/`、
  甚至直接粘贴 `http://localhost:8080/v1/chat/completions`，都会自动归一化。
- **「测试连接」会把服务端的模型列表拉下来**填进下拉框 —— 本机模型名
  （`qwen2.5:7b`、`llama-3.2-3b-instruct`）没人记得住，不用手打。
- **尽量选非推理（instruct）模型**：推理模型会先花时间"思考"，实测修一句话要
  6–18 秒；本机 7B instruct 通常在 1 秒内。差别非常明显。
- **超时**默认 15 秒。本机小模型第一次调用要加载权重，慢的话把它调大。
- **LLM 挂了不会影响使用**：客户端按失败原因分类处理 ——
  **Key 错/连不上**会明确提示你去改配置；**推理模型把预算花光**会单独报出来
  （提示调大 `max_tokens` 或换模型）；**超时/限流/5xx** 静默降级，
  直接注入识别原文。绝不会因为模型不可用就丢了你刚说的话。

命令行自测（会真的调用模型）：

```bash
python tools\test_llm_live.py --list-models        # 看看服务端有哪些模型
python tools\test_llm_live.py                      # 跑修正/翻译用例
python tools\test_llm_live.py --ui                 # 验证设置界面的「测试连接」按钮
python tools\test_llm_live.py --base-url http://localhost:11434/v1 --model qwen2.5:7b
```

## 六、架构

```
Windows 客户端                     WSL 识别服务
├── hotkey.py    全局热键          r2t2_stream_server.py
├── audio.py     16k 采集           └─ R2T2 llama.cpp 流式后端
├── asr.py       WS 客户端 + 重连        （模型常驻显存 ~2.5 GB）
├── overlay.py   不抢焦点悬浮窗
├── llm.py       LLM 修正/翻译
├── translator.py 增量流式翻译
├── inject.py    文本注入
├── settings_ui.py 设置界面
└── app.py       主程序
```

**为什么必须两段式**：识别模型是 Linux + CUDA 的编译产物，Windows 调不动；
而热键/麦克风/悬浮窗/注入又是 Windows 独有的能力。两边靠 WSL2 的 localhost 转发连接。
细节见 [docs/architecture.md](docs/architecture.md)。

## 七、开发计划

### v0.2 —— 接入真实大模型 ✅ 已完成

v0.1 时这两个引擎的代码已写好并通过离线自测（用本地假服务端验证失败分类、输出校验、
增量翻译的触发条件），但**还没接过真实模型**。v0.2 把它接到了真模型上：

- ✅ 接入任意 OpenAI 兼容接口，本机（Ollama / LM Studio / llama.cpp server / vLLM）
  与云端一视同仁；设置界面提供预设、模型列表下拉、连接自检
- ✅ **文字修正**：去口水词（嗯/啊/那个）、补标点、纠正同音错别字、数字规范化
- ✅ **流式翻译**：原文与译文并行滚动，按句末标点/静默 1.5 s/长度阈值触发，
  **已落定的段落永不重译**
- ✅ 失败降级：认证/连接错误 → 提示改配置；超时/限流/5xx → 静默回退识别原文
- ✅ 真机自测工具 `tools/test_llm_live.py`（含设置界面「测试连接」的验证）

真机实测（同一台 RTX 4070 Laptop，模型为云端 OpenAI 兼容接口）：

| 用例 | 结果 | 耗时 |
|---|---|---|
| 「嗯那个我们今天就是测试一下这个语音输入法看看它能不能把口水词去掉」 | →「我们今天测试一下这个语音输入法，看看它能不能把口水词去掉。」 | 2.0 s |
| 「我们要用快首科技的产品」＋热词「快手科技」 | →「我们要用快手科技的产品来做这个项目。」 | 2.0 s |
| 「这个项目大概要三个月时间预算二十万左右」 | →「这个项目大概要 3 个月时间，预算 20 万左右。」 | 1.1 s |
| 「帮我把这个 API 的 end point 改成那个 post 请求」 | →「帮我把这个 API 的 endpoint 改成 POST 请求。」 | 1.8 s |
| 整段中译英 | → 通顺英文，要点齐全 | 1.1 s |

### v0.3 —— 修正模式 ✅ 已完成（当前版本）

v0.2 把模型接上了，但「修正」只有一种力度：同一个 Prompt 既要照顾"别动我的字"，
又要照顾"整理成能发出去的文字"，结果两边都不讨好。

- ✅ **三种内置力度**：不整理（只修识别错误）/ 轻度整理（去口水词、补标点、保留措辞）/
  深度整理（整理成书面表达）
- ✅ **卡片式选择**：沿用参考图的做法 —— 名称 + 一句话说明 + 效果示例，
  三种力度并排对比，选中的那张高亮
- ✅ **自定义模式**：自己起名、写说明与 Prompt，与内置模式同等对待；
  内置的删不掉（它们是产品默认行为）
- ✅ **去除结尾句号**开关
- ✅ 配套修掉一个真问题：**推理模型会先把 `max_tokens` 花在思考上**，预算不够时
  `content` 为空，用户看到的是"修正在报输出长度失控"——现在单独分类并说明
  「调大 max_tokens 或换非推理模型」

### v0.4 —— 打包与易用性

- 一键安装包（不再需要用户自己建 venv）
- 开机自启 + 托盘图标
- 音量波形、更细的悬浮窗主题
- 词典热词的实际纠错（当前只作为 context 传给识别引擎）

### 长期

- 更多语言识别（模型本身支持中/英/日/韩等）
- macOS / Linux 客户端的可行性（识别服务协议已经是跨平台的）

## 八、许可

本项目代码 **MIT**。运行时依赖与设计参考的许可见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（注意区分：R2T2 **代码** Apache-2.0，
**权重** NetEase Model Use License）。

---

# English

## What is EtherealFlow

**A true-streaming voice input method that runs entirely on your own machine.**

Hold a hotkey and speak — **you watch the words appear on screen as you talk**. Release the key
and the cleaned-up text is injected into whatever input box you were already using.
Speech never leaves your computer.

![true streaming demo](docs/assets/streaming-demo.gif)

## Why "true" streaming

Most voice input tools transcribe **after** you stop talking: you hold, speak, release, wait —
then the whole sentence pops up at once. If you misspoke, you only find out at the end.

EtherealFlow transcribes **while you speak**. The recognition backend (Confucius4-R2T2) is a
genuinely append-only streaming model: text already emitted is never revised, so what you see
grows monotonically instead of flickering. That's the difference between *true* streaming and
chunked pseudo-streaming.

The name: **EtherealFlow = Ethereal (缥缈) + Flow**. In Chinese the single character 「流」 carries
both meanings — **flow** (the psychology concept, 心流) and **streaming** (流式).

## Runs locally — your voice stays on your machine

```
┌──────────── your Windows PC ─────────────┐
│  microphone → client ─┐                  │
│                       │ ws://127.0.0.1:18300
│  overlay ←────────────┘                  │
│                       ↓                  │
│        ASR service inside WSL2           │
│        Confucius4-R2T2 (llama.cpp+CUDA)  │
│        ~2.5 GB VRAM, always resident     │
└──────────────────────────────────────────┘
        no internet · no cloud service
```

- **Audio never leaves the machine** — it is sent to `127.0.0.1:18300`, a service on your own PC.
- **No API key, no per-minute billing** — the model weights are local.
- **Bring your own ASR** — the client speaks one WebSocket protocol
  (see [docs/protocol.md](docs/protocol.md)); swap in a self-hosted engine by replacing the server.

> ⚠️ **Model weights are not bundled** (they're GB-scale). The recognition engine is
> [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2) by NetEase Youdao:
> code is Apache-2.0, but **weights are under the NetEase Model Use License** — a different
> licence; check it yourself before commercial use. Deployment: [docs/development.md](docs/development.md).

## Features

| Feature | Status |
|---|---|
| Global push-to-talk hotkey (hold / release / Esc to cancel) | ✅ |
| 16 kHz mono capture (auto-resamples if the device refuses 16 kHz) | ✅ |
| **Streaming display** in a non-focus-stealing overlay | ✅ |
| Text injection (clipboard+Ctrl+V, typing fallback, clipboard restore) | ✅ |
| Automatic long-utterance segmentation | ✅ |
| Auto reconnect / auto recovery after the service crashes | ✅ |
| Settings UI | ✅ |
| **LLM text correction** (de-filler, punctuation, homophones, number normalisation) | ✅ tested against a real model |
| **Three correction strengths** (none / light / deep, card picker) | ✅ |
| **Custom correction modes** (your own name, description and prompt) | ✅ |
| Strip the trailing full stop | ✅ |
| **Streaming bilingual translation** (finalised segments are never re-translated) | ✅ tested against a real model |
| Any OpenAI-compatible endpoint (local Ollama / LM Studio / llama.cpp / vLLM / cloud) | ✅ presets, model list, connection check |

## Measured performance (RTX 4070 Laptop / WSL2)

| | |
|---|---|
| Model load | 4.3 s |
| 6.74 s audio, streaming decode | 0.56× realtime, first character at **1.20 s** |
| 33.70 s audio | 0.88× realtime |
| 53.92 s audio | **1.15× realtime (falls behind)** → client auto-segments at 30 s |
| After segmentation (8 s per segment) | **0.54× realtime** |
| Windows → WSL handshake | 17 ms |
| Bundle size | 58 MB (Python included, no install needed) |

## Quick start

```bash
# 1) deploy the ASR service into WSL (adds a directory; touches nothing existing)
wsl.exe -d Ubuntu -u <user> -- bash -l "/mnt/d/<repo>/scripts/deploy_wsl.sh" --install-service --restart

# 2) run the client
python -m venv .venv
.venv\Scripts\python.exe -m pip install numpy sounddevice websockets
.venv\Scripts\python.exe -m client.app

# 3) hold Ctrl+Win, speak, release.
```

Settings: `python -m client.app --settings` ·
Self-tests (no keypress, no speech, no API key needed): `python tools\run_all_tests.py`

### Optional: connect an LLM for correction / translation

Off by default. Open **Settings → 修正 (Correction)**, pick a preset under 模型接入, hit
**测试连接 (Test connection)**. Then choose a correction strength by clicking a card:

| Mode | What it does | Same sentence, three ways |
|---|---|---|
| **不整理 (none)** | fixes only clear recognition errors, changes nothing else | 嗯我跟你说明天**会议**吧不对不对先讨论排期再聊产品方案 |
| **轻度整理 (light)** | drops fillers and self-corrections, adds punctuation, keeps your wording | 我跟你说，先讨论排期，再聊产品方案。 |
| **深度整理 (deep)** | keeps the meaning, rewrites into written prose | 我跟你说，先讨论排期，再聊产品方案。 |

(source: `嗯我跟你说明天回议吧不对不对先讨论排期再聊产品方案`)

You can also **add your own modes** — name, one-line description, example and prompt
(`{text}` is the transcript). Custom modes are treated exactly like the built-in ones.
A separate toggle removes a trailing full stop, for when you are dictating mid-thought.

| Scenario | Preset | Note |
|---|---|---|
| **Fully local** (recommended) | Local · Ollama | run `ollama serve` and `ollama pull qwen2.5:7b` first |
| **Fully local** | Local · LM Studio | load a model in the app and start its local server |
| **Fully local** | Local · llama.cpp server / vLLM | just point at the address they listen on |
| Cloud | OpenAI / DeepSeek / Kimi / GLM / Qwen / SiliconFlow | pick a preset, then paste an API key |

Things we learned the hard way:

- Local servers need **no API key** — leave it blank. The client sends no `Authorization`
  header at all when the key is empty.
- **Any reasonable address works**: `http://localhost:11434`, `http://localhost:1234/v1/`, or even a
  pasted `http://localhost:8080/v1/chat/completions` — all normalised automatically.
- **Test connection** pulls the server's model list into the dropdown, so you never have to
  remember names like `qwen2.5:7b`.
- **Prefer a non-reasoning instruct model.** Reasoning models "think" first: measured 6–18 s to
  correct one sentence, versus well under a second for a local 7B instruct model.
- **A broken LLM never costs you a sentence**: auth/connection problems tell you to fix the config;
  a reasoning model that burns its whole `max_tokens` budget on thinking is reported as such
  (raise `max_tokens` or switch model); timeouts, rate limits and 5xx silently fall back to the
  raw transcript.

```bash
python tools\test_llm_live.py --list-models     # what models does the endpoint offer?
python tools\test_llm_live.py                   # run the correction/translation cases
python tools\test_llm_live.py --ui              # verify the Settings "Test connection" button
```

## Roadmap

**v0.2 — connect a real LLM: text correction and translation. ✅ Done (current release).**
v0.1 already shipped both engines in code, passing offline tests, but they had never been pointed at a
real model — so they shipped disabled. v0.2 wires them to real models: any OpenAI-compatible endpoint,
local (Ollama / LM Studio / llama.cpp server / vLLM) or cloud, with presets, a model-list dropdown and
a connection check in Settings. Correction does de-stuttering (嗯/啊/那个), punctuation, homophone
fixes and number normalisation; **streaming translation** scrolls the original and the translation
side by side and never re-translates an already-finalised segment. A broken LLM never costs you a
sentence: auth/connection errors tell you to fix the config, while timeouts, rate limits and 5xx
silently fall back to the raw transcript.

**v0.3 — correction strengths. ✅ Done (current release).** v0.2 connected the model, but correction
had a single strength: one prompt had to serve both "don't touch my words" and "make this sendable",
and did neither well. v0.3 adds three built-in strengths (none / light / deep) presented as
side-by-side cards with a name, a one-line description and a worked example, plus user-defined modes
with their own name and prompt, and an option to drop the trailing full stop. It also fixes a real
issue found while testing: reasoning models spend their `max_tokens` budget on thinking first, so the
answer never arrives — that failure mode is now classified and explained instead of surfacing as a
baffling "output too long".

**v0.4 — packaging and ergonomics.** One-click installer, autostart + tray icon, richer overlay.
**Longer term.** More recognition languages (the model already supports several), and
macOS/Linux clients (the service protocol is already cross-platform).

## License

Code is **MIT**. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for runtime dependencies
and design references — note that Confucius4-R2T2 *code* is Apache-2.0 while its *weights* are
under the NetEase Model Use License.
