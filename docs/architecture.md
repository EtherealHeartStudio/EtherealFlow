# CherryVoice 架构说明

## 1. 为什么必须两段式

```
┌──────────── Windows 客户端（Python 3.13）────────────┐
│  全局热键 ─→ 麦克风采集 ─→ WebSocket 发 16k PCM 块     │
│                              ↓                       │
│  悬浮窗 ←── 累积文本 ─────────┘                       │
│  松开热键 → LLM 修正/翻译 → 注入当前输入框             │
└───────────────────────┬──────────────────────────────┘
                        │ ws://127.0.0.1:18300
┌───────────────────────▼──────────────────────────────┐
│  WSL 识别服务（Python 3.12）：R2T2 llama.cpp + CUDA   │
└──────────────────────────────────────────────────────┘
```

不是随意的技术选择，而是被环境逼出来的：

* **识别模型只能在 WSL 里跑** —— Confucius4-R2T2 依赖 Linux + CUDA，
  且其 llama.cpp 后端是编译好的 `.so`，Windows 侧无法直接调用。
* **热键/麦克风/悬浮窗/注入只能在 Windows 侧** —— 这些都要碰 Win32 消息、音频栈、
  窗口样式，WSL 里做不到。
* **两边靠 WSL2 的 localhost 转发连起来** —— WSL 内监听 `0.0.0.0:18300`，
  Windows 直接访问 `127.0.0.1:18300`，实测握手约 17 ms。

## 2. 模块职责

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `wsl/r2t2_stream_server.py` | 把官方 llama.cpp 流式后端包成 WebSocket 服务 | 每连接一个 `streaming_state`；GPU 推理全局串行；启动时预加载模型 |
| `client/hotkey.py` | 按住/松开全局热键 | **两种后端**：`GetAsyncKeyState` 轮询（默认）与 `WH_KEYBOARD_LL` 钩子 |
| `client/audio.py` | 麦克风采集 | 16 kHz/单声道/int16；打不开 16 kHz 就退到设备默认率并软件重采样 |
| `client/asr.py` | 识别服务客户端 | 长连接 + 自动重连；所有帧走同一队列保证顺序；长语音**分段重建** |
| `client/overlay.py` | 不抢焦点的悬浮窗 | `WS_EX_NOACTIVATE` 必须加在**顶层窗口**上 |
| `client/win32.py` | Win32 封装（DPI/窗口样式/前台/焦点） | 只用 ctypes，零第三方依赖 |
| `client/llm.py` | LLM 修正/翻译 | 失败分类 + 输出校验 |
| `client/translator.py` | 增量流式翻译 | 只翻译新增部分；分段 append-only |
| `client/inject.py` | 文本注入 | 剪贴板 + Ctrl+V 主方案，逐字键入兜底，剪贴板保护 |
| `client/app.py` | 主程序：把上面串起来 | 主线程跑 tkinter，其余全在后台线程 |
| `client/config.py` | 配置 | 存 `%APPDATA%/CherryVoice/`，不进仓库 |
| `client/settings_ui.py` | 设置界面（FR-7 六分组） | `apply()`/`collect()` 与界面分离，便于脱机测试 |

## 3. 一次说话的完整时序

```
用户按下 Ctrl+Win
  ├─ PollingHotkey 检测到边沿 → on_press
  ├─ overlay.show_listening()          （SW_SHOWNOACTIVATE，不抢焦点）
  ├─ asr.begin_session()               （start 帧先于任何音频进队）
  └─ MicCapture.start()                （16k 单声道，切 160 ms 块）

每 160 ms
  ├─ MicCapture → asr.feed(pcm)        （只入队，绝不阻塞采集线程）
  └─ AsrClient 工作线程 → ws.send
       ← {"type":"partial","text":"累积全文"}
       → overlay.set_text()            （线程安全：走队列，主线程消费）
       → translator.update()           （翻译线程自己决定何时翻）
       超过 max_utterance_sec → asr.rotate()（分段，见 §5）

用户松开
  ├─ MicCapture.stop() → asr.end_session()
  ├─ → {"type":"finish"}
  └─ ← {"type":"final","text":...}
       ├─ 翻译开启 → translator.finish() → 提交**译文**
       └─ 否则     → llm.correct()      → 提交**修正后文本**
                     失败按分类降级 → 提交识别原文
  最后 Injector.inject(text)
```

## 4. 四个"踩过才知道"的设计点

### 4.1 `WS_EX_NOACTIVATE` 必须加在顶层窗口

tkinter 的 `winfo_id()` 返回的是 **`TkChild` 子窗口**，真正会被激活的是父级
`TkTopLevel`。把扩展样式加在子窗口上**完全无效**，悬浮窗照样抢焦点，
后果是文本被注入到错误的窗口。见 `win32.top_level_window()`。

### 4.2 空 partial 是正常中间态

后端在还没解析出 `<asr_text>` 标签时会把 `state.text` 清空。
服务端**过滤空串**并保留最后一个非空文本兜底，客户端也不该因为收到空串就清屏。

### 4.3 llama.cpp 的日志关不掉

它在 C 层直接往 fd 1/2 打上千行。Python 侧没有开关
（`Qwen3ASRNative` 构造函数不接受 verbose 参数），只能启动时 `dup2` 到
`logs/native.log`，我们自己的日志单独写 `logs/service.log`。

### 4.4 keepalive 超时不能太短

长语音时客户端可能因服务端处理不过来而在 `sendall` 里阻塞，一时顾不上回 pong。
默认 20 s 的超时会把这种**正常背压**误判成死连接并断开（实测 53.9 s 音频报 1011）。
服务端放宽到 60 s，客户端每轮最多连发 8 帧就回去收一次。

### 4.5 低级钩子的 ctypes 签名不能省

`CallNextHookEx` 不声明 `argtypes` 时，64 位的 `lparam` 会被猜成 `c_int` 而溢出；
钩子过程抛异常等价于返回 0，而**返回 0 表示"已处理"，会把每一个按键都吞掉**。
另外钩子报的修饰键是**左右专用**的（`VK_LCONTROL=0xA2`），不做归一化的话
组合键永远配不上。详见 `docs/development.md` §7。

## 5. 长语音：为什么要分段重建会话

流式解码每块都要**重新编码累积的全部音频**，成本随长度增长。实测：

| 音频 | 折合实时 |
|---|---|
| 6.74 s | 0.56× |
| 33.70 s | 0.88× |
| 53.92 s | **1.15×（跟不上）** |

拐点在 ~45 秒。所以客户端默认连续说 **30 秒**就主动
「结束当前段 → 累积 final → 立刻开新段」，对用户无感。
同一份 33.7 s 音频按 8 s 分段后，解码从 0.88× 降到 **0.54×**。

代价是段与段之间可能有轻微接缝（切点可能落在词中间），所以上限不宜设太小。

## 6. 失败与降级策略

| 故障 | 行为 |
|---|---|
| LLM 鉴权失败 / 连不上 | **提示用户改配置**，本次注入识别原文 |
| LLM 超时 / 限流 / 5xx | **静默降级**，注入识别原文 |
| LLM 输出复读、吐热词表、超长 | 输出校验拦下，退回原文 |
| 翻译全失败 | 提交识别原文（**绝不提交空串**） |
| 识别服务连不上 | 客户端自动重连（指数退避）；采集时的音频队列有上限，超出丢最旧的并告警 |
| 识别服务**崩溃** | systemd `Restart=always` 自动拉起，实测**约 13 秒**后端口恢复（`RestartSec=5` + 模型重载）；客户端在此期间自动重连 |
| 说话中途断线 | 重连后**用原参数补发 `start`**，语言提示与热词不丢（见 `AsrClient._session_args`） |
| 识别结果为空 | 不注入任何东西 |
| 配置损坏 / 数字填错 | 回退默认值，不崩 |
