# CherryVoice 开发指南

## 1. 环境准备

### Windows 侧（客户端）

```bash
# Python 3.13，项目内虚拟环境（不要用全局安装）
python -m venv .venv
.venv\Scripts\python.exe -m pip install numpy sounddevice websockets
.venv\Scripts\python.exe -m pip install pyinstaller      # 只有打包才需要
```

### WSL 侧（识别服务）

识别引擎**已经装好**，不需要重装。要点见 `docs/protocol.md` 与需求文档 §6：

```bash
# 运行前必须设置（venv 里有 cudart/cublas，WSL 只有驱动层 libcuda.so）
export LD_LIBRARY_PATH=/home/<user>/Confucius4-R2T2/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/home/<user>/Confucius4-R2T2/.venv/lib/python3.12/site-packages/nvidia/cublas/lib
export GGUF_DIR=/home/<user>/models
```

部署服务（**只新增目录，不改动任何现有文件**）：

```bash
wsl.exe -d Ubuntu -u <user> -- bash -l "/mnt/d/<仓库路径>/scripts/deploy_wsl.sh" --install-service --restart
```

服务文件在 `~/.config/systemd/user/r2t2-stream.service`，
日志在 `~/cherryvoice/logs/`（`service.log` 是我们自己的，`native.log` 是 llama.cpp 的）。
开机自启还需要一次性 `sudo loginctl enable-linger <user>`。

## 2. 目录结构

```
run_client.py           打包入口（PyInstaller 只吃脚本路径，不支持 -m）
client/                 Windows 客户端
  config.py             默认值 / 加载 / 保存（存 AppData，不进仓库）
  win32.py              Win32 ctypes 封装（DPI / 窗口样式 / 前台 / 焦点）
  audio.py              麦克风采集
  asr.py                识别服务客户端（重连 + 长语音分段）
  hotkey.py             全局热键（轮询 / 低级钩子）
  overlay.py            不抢焦点悬浮窗
  llm.py                LLM 修正/翻译（失败分类 + 输出校验）
  translator.py         增量流式翻译
  inject.py             文本注入（剪贴板 / 逐字键入）
  settings_ui.py        设置界面
  app.py                主程序
wsl/                    WSL 识别服务
tools/                  自测与诊断工具（见 §3）
scripts/                部署与打包脚本
docs/                   本目录
```

## 3. 自测：全部不需要人按键 / 说话 / API Key

```bash
python tools\run_all_tests.py                     # 一键跑完（CI 也是跑这个）
python tools\run_all_tests.py --fast              # 跳过较慢的界面/回放类
python tools\run_all_tests.py --only llm          # 只跑名字含关键词的

python tools\test_win32_signatures.py             # Win32 ctypes 签名审计
python tools\test_overlay_focus.py --shot o.png   # 悬浮窗不抢焦点（含阳性对照）
python tools\test_mic_path.py --wav a.wav         # 真实音频链路（虚拟声卡回环）
python tools\test_hotkey.py                       # 热键：逻辑 + 注入探测
python tools\test_injection.py                    # 注入：剪贴板规则 + Ctrl+V / 键入
python tools\test_llm.py                          # LLM 失败分类与输出校验（假服务）
python tools\test_translator.py                   # 增量翻译：三种触发 + 只翻新增
python tools\test_translate_flow.py --wav a.wav   # 翻译接到主流程
python tools\test_closed_loop.py a.wav            # 完整闭环（假 LLM）
python tools\test_settings.py                     # 设置界面配置往返（含截图）
python tools\test_stream_client.py --wav a.wav    # 识别服务验收（零依赖）
python tools\probe_key_injection.py 4             # 探测合成按键能否驱动两种后端
python tools\check_release.py                     # 发布前安全检查
```

**写测试时的第三条经验**：**Win32 的坑要写成自动化审计，不要靠 review。**
`test_win32_signatures.py` 检查每个用到的函数是否显式声明了 `argtypes`/`restype` ——
漏声明只在 64 位 + 句柄值较大时才发作，表现是"行为诡异"而不是报错
（悬浮窗定位错、抢不到前台、钩子不触发）。这个审计刚写完就抓到了我自己引入的
一处回归：给 `keybd_event` 补 `argtypes` 时把第 4 个参数写成 `POINTER(ULONG)`，
结果所有传 `0` 的调用点立刻报 `expected LP_c_ulong instance instead of int`。
**给已有调用点补签名时，要回头看所有调用点传的实参类型。**

**写测试时的两条经验**（都是踩过才总结的）：

1. **要有阳性对照。** 「没发现问题」可能只是检测器坏了 ——
   `test_overlay_focus.py` 会另外开一个普通窗口确认检测器真能发现抢焦点行为。
2. **区分「环境做不到」和「代码错了」。** 合成按键在受管环境里可能根本投递不到桌面，
   这时应该标 `[SKIP]` 并写明原因，**不要标 `[PASS]`**，也不要算成 `[FAIL]`。

## 4. 常用调试手段

```bash
# 只跑识别+决策，不往任何窗口灌字（最安全的自测方式）
python -m client.app --replay a.wav --exit-after-final --inject-mode none --no-llm

# 不开 LLM，直接注入识别原文
python -m client.app --replay a.wav --exit-after-final --no-llm

# 强制用另一种热键后端 / 指定输入设备
python -m client.app --hotkey-backend hook
python -m client.app --list-devices

# 打开设置界面
python -m client.app --settings

# 服务端脱离网络自检
python wsl/r2t2_stream_server.py --selftest-wav /path/test.wav
```

* 客户端日志：`%APPDATA%\CherryVoice\logs\client.log`
* 服务日志：`~/cherryvoice/logs/service.log`；llama.cpp 噪音在 `native.log`
* 想看 llama.cpp 原生日志（排查模型问题）：启动服务前设 `R2T2_STREAM_NATIVE_LOG=1`
* 配置文件：`%APPDATA%\CherryVoice\config.json`，**只写与默认值不同的键**

## 5. 改代码时要留意的地方

* **别把重活放进热键/采集回调线程。** `MicCapture.on_block` 与
  `AsrClient` 的回调都在后台线程里，UI 必须走 `Overlay.post()` 投递到主线程。
* **`update()` / `feed()` 只许入队。** 一旦在里面同步调用 LLM 或做阻塞 I/O，
  识别线程就被拖住，悬浮窗会卡住不出字。
* **新增配置项要同时改三处**：`config.py` 的 `DEFAULT_CONFIG`、
  `settings_ui.py` 的 `apply()`/`collect()`，以及使用它的模块。
  `test_settings.py` 会检查界面往返一致性。
* **改了提示词模板要跑 `test_llm.py`。** 热词必须放在「原文：」**之前** ——
  放在后面时模型会把热词当成正文，甚至直接把热词表当结果吐回来（实测）。
* **提交前必须跑 `check_release.py`。** 退出码非 0 就不许发布。
  确需豁免的行加注释 `release-check: allow`。

## 6. 已知限制

* **合成输入在本环境下不稳定**：`SendInput` 总是返回成功，但按键**时通时不通** ——
  投递到自己进程的前台窗口（Ctrl+V / 逐字键入）稳定可用，投递到系统级
  （`GetAsyncKeyState`、全局钩子）则时有时无。所以热键的**真实按键**验收
  仍需人在正常桌面会话完成。探测脚本：`tools/probe_key_injection.py`。
* **前台锁**：`force_foreground()` 用了四级降级，但仍不保证一定抢得到前台
  （例如资源管理器反复抢占时）。这是它只作**兜底**的原因。
* **剪贴板拦截层**：某些环境下 `SetClipboardData` 返回成功但回读仍是旧值。
  注入器因此改成「写完回读确认」才置 `restored=True`。
* **OCR/长语音**：超过 ~45 秒的**单段**会跟不上实时，靠客户端 30 秒分段规避。
* 模型权重按 **NetEase Model Use License**，与本仓库的 MIT 不同，商用前请自行确认。

## 7. 钩子后端的两个坑（都已修，写在这里免得再踩）

用 `WH_KEYBOARD_LL` 时必须注意：

1. **`CallNextHookEx` 必须声明 `argtypes`。** 不声明的话 ctypes 按 Python 值猜类型，
   `lparam` 是 64 位指针却被猜成 `c_int` → `OverflowError`。而钩子过程抛异常
   等价于返回 0，**返回 0 表示"已处理"，Windows 会把每一个按键都吞掉**。
   实测现象：回调进来了 6 次、每次都崩、一个热键事件都没记下来。
   这些签名现在放在 `client/hotkey.py` **模块级** —— 放在 `_run()` 里的话，
   任何在启动前调用 `_callback` 的路径（比如单元测试）都会重现这个 bug。
2. **钩子报的是左右专用的修饰键。** 按住左 Ctrl 收到的是 `VK_LCONTROL=0xA2`，
   而不是 `VK_CONTROL=0x11`；Alt 是 `0xA4`，Shift 是 `0xA0`。
   不做归一化，组合键**永远配不上**，表现为"钩子装上了但按了没反应"。
   归一化在 `hotkey.canonical_vk()`，回归测试在 `tools/test_hotkey.py` 的 C 段。

顺带一提：`SetWindowsHookExW` 也**必须声明 restype**，否则 64 位句柄被截成 32 位，
卸载钩子会静默失败。
