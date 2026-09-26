# EtherealFlow 前期技术调研（源码级）

方式：GitHub API 取文件树 [cite:b9a62c46-1][cite:b9a62c46-2][cite:b9a62c46-3] 后逐文件下载精读。push-2-talk/VoiceX 为完整 clone，typeflux 为 raw 单文件抓取（其 zip 下载失败，未做全仓 clone）。

## 各项目关键发现

### push-2-talk（Rust/Tauri，MIT）
- **Windows 上故意不用低级钩子**：`src-tauri/src/hotkey_service.rs` 注释「Windows：使用 GetAsyncKeyState 轮询按键状态，避免低级 hook 的兼容性问题」，`HOTKEY_POLL_INTERVAL_MS = 10`，rise/fall 边沿做 PTT；非 Windows 才 `rdev::listen`。另有独立 watchdog 线程复检物理键，动机注释写明是 rdev 漏 `KeyRelease` 的 Ghost Key。
- **键位方案**：`config.rs` 的 `HotkeyKey` 枚举共 **72 个变体**（8 修饰键含 Meta 左右、F1–F12、Space/Tab/CapsLock/Escape、A–Z、0–9、方向键、8 个编辑键）；`hotkey_service.rs::is_key_physically_down()` 建「变体→VK 码」表，`is_hotkey_pressed_strict()` 要求「全部按下且**无额外修饰键**」。默认听写键 `ControlLeft+MetaLeft`（Ctrl+Win），AI 助手 `AltLeft+Space`，松手模式独立键默认 `F2`。
- **悬浮窗**：`src-tauri/tauri.conf.json` 的 `overlay` = 200×80、`decorations:false`、`transparent:true`、`shadow:false`、`alwaysOnTop:true`、`skipTaskbar:true`、**`focusable:false`**。状态机在 `src/windows/OverlayWindow.tsx`：`recording`（9 条波形）/ `recording + locked`（松手模式，取消+完成按钮）/ `transcribing`（加载动画），由 `recording_started/locked/stopped/transcribing/transcription_complete/error/transcription_cancelled` 事件驱动；另有 15s 转写超时、60s 松手模式超时兜底。
- **注入**：`text_inserter.rs` = 存剪贴板 → `set_text` → sleep 50ms → `SendInput` Ctrl+V → sleep 150ms → **无条件**还原；`win32_input.rs` 的 `KEY_DELAY_MS = 15`。`clipboard_manager.rs` 用 RAII `ClipboardGuard`（`Drop` 自动还原），`get_selected_text()` 先清空剪贴板再 Ctrl+C，靠「是否变非空」判断有无选中内容。
- **目标窗口**：`lib.rs` 在**热键按下瞬间**存 `get_foreground_window()` 到 `target_window_start`；`pipeline/focus.rs` 隐藏悬浮窗 → sleep 50ms → `restore_focus_with_verify(hwnd, 3)` → sleep 100ms。`win32_input.rs::force_foreground_window` 三级降级：`SetForegroundWindow` → `AttachThreadInput` → 敲一下 Alt（`keybd_event`），每次重试 sleep 30ms。
- **完全不按目标应用区分**：全仓 grep `notepad/wechat/msedge/winword` 零命中（唯一带 app 名的 `asr/doubao_ime.rs` 里 `"com.android.chrome"` 属 ASR 协议字段）。
- 附带：`src-tauri/src/tnl/` 是「ASR 与 LLM 之间的确定性规则层」（归一化 → 分词 → 技术片段识别 → **仅片段内**口语符号映射，`rules.rs` 含「点→. / 艾特→@ / 斜杠→/」及繁体）；`asr/realtime/qwen.rs` 用 `input_audio_buffer.append/commit`、`turn_detection: null` 禁 VAD 改手动 commit、结果超时 10s、连接池空闲 180s 复用。

### VoiceX（Rust/Tauri，MIT）
- **热键**：`hotkey/manager.rs` 用 `rdev::grab`；vendored `vendor/rdev/src/windows/{listen,common}.rs` 确认是 `SetWindowsHookExA(WH_KEYBOARD_LL, …)` + `GetMessageA` 消息泵。回调返回 `None` 即吞键（`if suppress { None } else { Some(event) }`），热键不漏给目标应用；macOS 另用 `CGEventSourceFlagsState` 取权威修饰键状态，因事件跟踪状态会 desync（源码含该类 bug 的诊断日志）。
- **三手势状态机**：`src-tauri/src/state.rs` 的 `HotkeySessionState { Idle, Pending, PushToTalk, HandsFree }`：按下进 `Pending` **并立刻开录**；`hold_threshold_ms = 1000` 内松手 → `HandsFree`（轻点=免提持续）；≥1s → `PushToTalk`（松手停）；`double_tap_window_ms = 400`，免提期间再短按 → 双击升级翻译。目标应用由 `session/handlers/hotkey.rs::capture_foreground_app()` 在按下瞬间捕获。
- **HUD 不抢焦点**：`hud/window.rs` 建窗 `.always_on_top(true).skip_taskbar(true).focused(false).visible(false)`，Windows 额外 `.transparent(true).shadow(false)`（注释：避免无边框窗口 1px 白边）。尺寸 STREAM 256×100 / BATCH 204×78 / CAPTION 680×140；位置**跟随光标所在屏幕**，注释明确不用 `current_monitor()`（隐藏时 HUD 停在启动屏）；期望尺寸放静态量 `DESIRED_BOUNDS`，**绝不回读窗口尺寸**（tao 的 `WM_DPICHANGED` 跨屏会永久扭曲物理尺寸）。
- **HUD 渲染**：`src/hud/hud.ts` 消费 `state:recognizing/correcting/error/caption/audio_level/audio_spectrum/countdown`，partial 与 final 分开存（`partialText` / `lastNonEmptyText`）；`hud.css` 流式 2 行 12px、caption 4 行 20px，Windows 半透明用 CSS alpha `rgba(43,43,43,0.88)`。
- **注入**：`injector/clipboard.rs` 两模式 `{ Pasteboard, Typing }`，默认 Pasteboard；`TYPING_MODE_MAX_CHARS = 500` 超长自动回退；macOS typing 按 20 字符分块（`CGEventKeyboardSetUnicodeString` 上限），含换行直接回退；`injector/mod.rs` 用全局 `INJECTION_MUTEX` 保证同时只有一次注入。
- **剪贴板还原最细**：`ClipboardBackup { Text | Image | None }`；写完 `verify_clipboard_text`（5 次 × 20ms）确认写入；粘贴前 `CLIPBOARD_PRE_PASTE_DELAY_MS = 120`(macOS)/80；粘贴后在**独立线程**等 `CLIPBOARD_RESTORE_DELAY_MS = 900`/500 再还原，且**仅当剪贴板仍等于本次文本**才还原（`restore_clipboard_if_unchanged`）。
- **按应用区分**：`foreground_app.rs` 的 `TextInjectionAppOverride { platform, app_name, match_kind, match_value, mode, skip_clipboard_restore }`，`match_kind ∈ {bundle_id, executable_path, process_name}`，匹配用 `overrides.iter().rev().find(...)`（后配置优先）；Windows 侧 `GetForegroundWindow → GetWindowThreadProcessId → OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) → QueryFullProcessImageNameW`。默认 `textInjectionMode: 'pasteboard'`、`textInjectionOverrides: []`——**无任何记事本/微信/VSCode/Chrome/Word 内置预设**，由用户在 `src/views/InputSettings.vue` 针对「最近出现过的目标应用」逐个配置。`skip_clipboard_restore` 专为远程桌面类剪贴板桥（变长延迟，定时还原会竞态）。
- **死代码**：`injector/windows_input.rs::send_unicode_text()`（`SendInput + KEYEVENTF_UNICODE`，含代理对）全仓无调用点，Windows typing 实际走 Enigo。

### typeflux（Swift，AGPL-3.0）
- **热键**：`Hotkey/EventTapHotkeyService.swift` 用 `CGEventTapCreate(options: .defaultTap)` 监听 keyDown/keyUp/flagsChanged（可消费），失败或未授权降级 `NSEvent.addGlobalMonitorForEvents`，并用 `PrivacyGuard.isAccessibilityGranted()` + 定时重试在授权后重启 tap；历史面板另用 Carbon `RegisterEventHotKey`。
- **手势仲裁**：`HotkeyGestureArbiter.swift` 的 `Phase { idle, pendingModifierActivation, active(action) }`，`doubleTapMaximumInterval = 0.45`；修饰键单键触发先挂起 0.22s（`modifierShortcutArbitrationDelay`）区分「轻点」与「组合键前缀」，注释强调该计时器**不拖延录音启动**；`RecordingStopGesture.swift` 独立跟踪物理按键集合，保证「启动录音时按住的键不会把自己停掉」。
- **不抢焦点（最值得抄）**：`Overlay/OverlayController.swift` 的 `OverlayPanel: NSPanel` 重写 `canBecomeKey` 返回 `allowsKeyboardFocus`（默认 false），`styleMask = [.nonactivatingPanel, .borderless]`，`level = .statusBar`，`hasShadow=false`、`isOpaque=false`、`becomesKeyOnlyIfNeeded=true`、`collectionBehavior=[.canJoinAllSpaces, .transient]`；展示一律 `orderFrontRegardless()`，源码注释：「**永远不要用 makeKeyAndOrderFront**，抢走 key window 会让原应用失焦丢选区，LLM 处理后无法写回」。`ignoresMouseEvents` 随 `metrics.interactive` 动态开关；胶囊宽 344pt、最多 3 行。
- **注入分层**：`TextInjection/TextDeliveryCoordinator.swift` 定义 `TextDeliveryResult { delivered(method), unconfirmed(method), notApplied(method) }`；resolve 目标 → `writeNative`(AX) → 仅当 `unsupported` 才走剪贴板 lease + paste → `observe` 用「精确范围替换」证据判定（最长 4 次 × 120ms）→ `finishClipboard(confirmed:)` 仅在事务仍持有载荷时还原。
- **`docs/TEXT_INSERTION_BEHAVIOR_PLAN_2026-09-05.md` 的主张**：普通听写**不绑定**启动时的 PID/窗口/选区，「生成结束后再解析当前活动输入」；「换个应用不是插入失败」；零长度选区就是正常插入；**「测量到真实兼容性需求前，不要先建 per-app adapter 框架」**——与 push-2-talk「按下存 hwnd + 还原焦点」是相反路线。
- **实测坑（同名 IMPLEMENTATION 文档）**：ChatGPT 输入框接受了 AX setter 调用但界面没提交（日志无 paste 派发却记成功）→ 删掉外部 setter；Zed/Sublime 只暴露 `AXWindow`，原 resolver 因此拒绝插入，改「保留不透明目标 + 身份/文本复验」后可用；把「无法验证」做成失败弹窗本身是缺陷。回归审计另确认过 whitespace 比较导致「未改变的选区被判失败」、以及「paste 派发即记为成功」。

## 可直接落地的设计建议

1. **PTT 走轮询**：`GetAsyncKeyState` + 10ms 边沿检测（push-2-talk 方案），天然不吞键、不会因回调超时被系统摘钩子；确需吞键时才另装 `WH_KEYBOARD_LL`（VoiceX/rdev 需在该线程跑 `GetMessage` 消息泵），两者不要并存。
2. **键位表照 `HotkeyKey` 的 72 项建三列表**（变体 → VK 码 → 显示名，Meta 左右独立），实装「严格匹配 = 不得有额外修饰键」，否则 Ctrl+Win 被 Ctrl+Shift+Win 误触发；再加独立松手模式键。
3. **悬浮窗不抢焦点（Windows 对应做法）**：`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`（`GetWindowLongPtr/SetWindowLongPtr` 设置），显示用 `ShowWindow(hwnd, SW_SHOWNOACTIVATE)` + `SetWindowPos(hwnd, HWND_TOPMOST, …, SWP_NOACTIVATE)`，**绝不用 `SW_SHOW`/`SetForegroundWindow`**；Qt 用 `Qt.Tool | FramelessWindowHint | WindowDoesNotAcceptFocus | WindowStaysOnTopHint`，Tk 用 `overrideredirect(True)` + `-topmost` 再补扩展样式。
4. **状态机照 VoiceX 抄**：`Idle → Pending →(1s)→ PushToTalk →(松手)→ Finalizing → Idle`，`Pending` 就开录（避免 1s 后才出声），松手按 `press_duration < threshold` 决定「轻点=免提」，再加 `double_tap_window_ms = 400` 承载第二功能；HUD 只消费事件（`state:*`），不自行推断状态。
5. **注入 = 剪贴板粘贴 + 三层保险**：(a) 写完先回读校验（5×20ms）；(b) 粘贴与还原放独立线程，还原前比对「剪贴板是否仍等于本次文本」；(c) Windows 用 `EnumClipboardFormats` + `GetClipboardData` 全格式备份，别只备 CF_UNICODETEXT。typing 仅作 ≤500 字符兜底，Windows 直接 `SendInput + KEYEVENTF_UNICODE` 逐码元发送（该 API 用法 VoiceX 已写好只是没接线）。
6. **目标窗口不必抢焦点**：我们 HUD 本就不激活，焦点从未离开目标窗口，正常路径只需注入前 `GetForegroundWindow()` 校验（变了就注入新窗口）；失败才降级 `restore_focus_with_verify(hwnd, 3)` 兜底，不要把它当默认路径。
7. **按应用区分只做「覆盖表 + 最近目标应用」**：`{platform, match_kind, match_value, mode, skip_clipboard_restore}`，后配置优先；**不要硬编码**记事本/微信/VS Code/Chrome/Word 方法表（三个项目都没这么做）。Windows 匹配 `process_name`（notepad.exe / WeChat.exe / Code.exe / chrome.exe / WINWORD.EXE），留 `executable_path` 兜底，远程桌面类目标设 `skip_clipboard_restore=true`。
8. **ASR 与 LLM 之间插确定性规范化层**（TNL 思路），减少 LLM 过度改写；**结果用三态而非 bool**（`delivered / unconfirmed / notApplied`），把「派发成功」与「确认写入」分开记录。

## License 注意

- **push-2-talk**：`LICENSE` 首行 `MIT License`，API `license.spdx_id = MIT` [cite:b9a62c46-4] → **代码可逐字复制**（保留版权与许可声明）。
- **VoiceX**：`LICENSE` 首行 `MIT License`（Copyright (c) 2025-2026 VoiceX Contributors），API `spdx_id = MIT` [cite:b9a62c46-5] → **代码可逐字复制**。注意 `src-tauri/vendor/rdev`、`vendor/audiopus_sys` 是 vendored 第三方（自带 LICENSE），应从上游获取。
- **typeflux**：`LICENSE` 为 **AGPL-3.0** 全文，README「## License / AGPL-3.0」，API `spdx_id = AGPL-3.0` [cite:b9a62c46-6] → **不可复制任何代码**（传染性同样约束开源项目：复制即需整体以 AGPL-3.0 分发并提供网络交互对应源码）。`OverlayController.swift`、`AXTextInjector*.swift`、`TextDeliveryCoordinator.swift` 只能作**设计思路**参考后自行实现。
- 结论：可逐字抄的只有 push-2-talk 与 VoiceX（均 MIT）；typeflux 一律只借思路与其文档经验。

## 我们应当避免的坑

- 别把 `WH_KEYBOARD_LL` 当 Windows 唯一 PTT 方案：push-2-talk 明确因「按键异常」改轮询；VoiceX 的 rdev 路线必须在钩子线程跑消息泵，否则钩子静默失效。
- 别把「抢焦点」三元 hack（`SetForegroundWindow`/`AttachThreadInput`/敲 Alt）当常态：它本是为补救「悬浮窗可能抢了焦点」，还会抖动输入法与 Alt 菜单。
- 别在注入后立刻无条件还原剪贴板：push-2-talk 的 `sleep(150ms)` + 无条件 `set_text(original)` 在慢应用/远程桌面上会过早还原，冲掉用户刚复制的内容或下一次注入；VoiceX 的 900ms 延迟 + 「内容未变才还原」和 typeflux 的 clipboard lease 都在修同一个 bug。
- 别把「粘贴已发出」记为成功，也别把 AX setter 返回值当写入确认（typeflux audit 的两次真实事故）；别用不精确证据判失败（其 whitespace 比较 bug 让「未改变的选区」被弹窗判失败）；判据须是「期望的精确范围替换结果」。
- 别把「无法验证」变成打扰用户的失败弹窗；别假设目标必须「插入」（零长度选区=正常插入，换应用不是失败）；别假设剪贴板只有文本。
- 别留半成品：VoiceX 的 `send_unicode_text` 无人调用，且 typing 与 pasteboard 是两套实现（typeflux audit 同样点了「dictation 与 persona 用不同 paste 策略」）。
- HUD 尺寸不要回读窗口（`WM_DPICHANGED` 会永久扭曲 Windows 物理尺寸）；隐藏的 HUD 不要用 `current_monitor()` 定位，要用光标所在屏幕；必须有超时兜底（15s 转写 / 60s 松手模式），否则 HUD 卡死。
- **更正任务描述两处**：(1) `config.rs` 的 `HotkeyKey` 我数到 **72** 个变体，未发现 73 项键位表；(2) 「三状态机」验证到的是 `OverlayWindow.tsx` 的 `recording / recording+locked / transcribing` 三态（另有 `isSubmitting` 子状态），非独立第三套状态机。
