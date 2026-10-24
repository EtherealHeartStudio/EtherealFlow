---
name: verify-etherealflow
description: 验证 EtherealFlow 客户端与识别服务的真实行为。当任务改动了 client/、wsl/、scripts/，或舰长问「能用了吗」「改好了吗」「验证一下」「跑一遍测试」时使用。跑离线自测套件 + 闭环回放 + 发布前安全检查，产出真实运行证据，而不是口头断言。
---

# 验证 EtherealFlow

这个技能回答一个问题：**改动真的生效了吗？**

不回答「编译通过吗」「代码看起来对吗」。只回答「跑起来是什么结果」。

## 何时用

- 改动了 `client/`、`wsl/`、`scripts/` 下任何文件之后
- 修完缺陷之后（必须重跑当初失败的用例）
- 舰长问「能用了吗 / 改好了吗 / 验证一下」
- 准备提交或发版之前

## 铁律

1. **没有实际运行输出 = 没有验证。** 不要用「应该可以了」代替证据。
2. **SKIP ≠ PASS。** 报告里 `[SKIP]` 要原样说明跳过原因，不许当成通过。
3. **WATCHDOG 不等于断言失败。** `test_overlay_focus.py` 的看门狗到点会打印完整
   结果并强制退出，那份结果就是全部断言结果。
4. **改了识别协议就必须两端验。** WSL 服务改了，客户端不改也要验客户端兼容性。
5. **发布前必须跑 `check_release.py`。** 它查 API Key 与个人路径泄露。

## 第 1 步：准备环境

```bash
cd <仓库根>            # 即 .../缥缈心流语音输入/EtherealFlow
.venv/Scripts/python.exe --version    # 确认项目 venv 存在
```

项目内 venv 是 `.venv`，**不要用全局 python**。

## 第 2 步：跑离线自测套件（主验证）

这是主力。全部不需要人按键、不需要人说话、不需要真 API Key。

```bash
.venv/Scripts/python.exe tools/run_all_tests.py
```

退出码 0 = 没有硬失败（SKIP 不算失败）。

变体：

```bash
.venv/Scripts/python.exe tools/run_all_tests.py --fast      # 跳过较慢的界面/回放类
.venv/Scripts/python.exe tools/run_all_tests.py --only llm   # 只跑名字含关键词的
```

进度会同时落盘到 `build/test_progress.log`。**命令卡住时先看这个文件**，
它能告诉你卡在哪一个。

## 第 3 步：按改动范围补跑专项

跑完主套件后，针对本次改动选跑：

| 改了什么 | 跑这个 |
|---|---|
| `client/win32.py`、`client/overlay.py` | `tools/test_win32_signatures.py` 和 `tools/test_overlay_focus.py --shot <路径>.png` |
| `client/hotkey.py` | `tools/test_hotkey.py`、`tools/probe_key_injection.py 4` |
| `client/inject.py` | `tools/test_injection.py`、`tools/test_inject_apps.py` |
| `client/llm.py` | `tools/test_llm.py` |
| `client/translator.py` | `tools/test_translator.py` |
| `client/asr.py` | `tools/test_asr_reconnect.py` |
| `client/audio.py` | `tools/test_audio_fallback.py`、`tools/test_mic_path.py --wav <音频>` |
| `client/config.py`、`client/settings_ui.py` | `tools/test_settings.py` |
| `wsl/` 或协议 | `tools/test_stream_client.py --wav <音频>` |
| 端到端 | `tools/test_closed_loop.py <音频>` |
| 发布前 | `tools/check_release.py` |

## 第 4 步：抓证据

**要留下可复核的证据，不要只在终端里跑完就算数。**

```bash
mkdir -p build/verify
.venv/Scripts/python.exe tools/run_all_tests.py 2>&1 | tee build/verify/<日期>-run_all_tests.log
.venv/Scripts/python.exe tools/test_overlay_focus.py --shot build/verify/<日期>-overlay.png
```

证据落在 `build/verify/`（该目录在 `.gitignore` 里，不会污染仓库）。

跑出来的截图、日志、退出码，**贴给舰长看**。

## 第 5 步：清理

只清理本次验证启动的东西：

- 杀掉验证过程起的服务进程（看 `runtime/pids/` 与 `build/` 下的 pid 文件）
- 不要动用户自己开的客户端实例
- 不要删 `build/verify/` 里的证据，交给舰长看过再清

## 识别服务（WSL 侧）单独验证

客户端测完不等于服务是好的。要单独验 WSL 服务时：

```bash
wsl.exe -d Ubuntu -u <user> -- bash -l "cd <工作区>/runtime && <启动服务命令>"
wsl.exe -d Ubuntu -u <user> -- bash -l "python wsl/r2t2_stream_server.py --selftest-wav <路径>/test.wav"
```

服务日志在 `~/etherealflow/logs/service.log`（自己的日志），
llama.cpp 噪音在 `native.log`。想看原生日志，启动前设 `R2T2_STREAM_NATIVE_LOG=1`。

**注意**：运行时权威说明在工作区 `runtime/README.md`，里面记着 CUDA 库路径、
vllm 替身等坑。改服务前先读它。

## 汇报格式

验证完给舰长这四件事：

1. **跑了什么** —— 命令 + 退出码
2. **结果** —— 通过 / 失败 / 跳过各多少，跳过的逐条说明原因
3. **证据在哪** —— `build/verify/` 下的日志与截图路径
4. **没验到什么** —— 明确说清哪些部分这次没覆盖到（不要含糊带过）

## 已知限制

- 合成按键在受管环境里可能投递不到桌面 → 相关测试标 `[SKIP]`，这是环境限制不是代码错
- 真实麦克风链路需要虚拟声卡回放，用 `test_mic_path.py --wav <音频>`
- 服务端验收用 `test_stream_client.py --wav`，零外部依赖
