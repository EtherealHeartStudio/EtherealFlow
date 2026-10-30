# -*- coding: utf-8 -*-
"""EtherealFlow · 设置热更新（BF-13.1）。

**为什么需要**：设置界面是**另一个进程**（``python -m client.app --settings``），
保存完配置后，正在跑的客户端根本不知道 —— 以前只有「重启才生效」这一条路，
用户改个模型名都得重启整个程序。

现在的做法：客户端**盯着配置文件**，发现变了就重新加载，把**能热更新**的部分
立刻换掉。

## 哪些能热更新，哪些不能

| 配置 | 能否热更新 | 说明 |
|---|---|---|
| ``llm.*``（模型/地址/Key/超时/模式/Prompt/max_tokens） | ✅ | 重建 LLM 客户端 |
| ``translate.*`` | ✅ | 下一次说话时用新参数 |
| ``inject.*`` | ✅ | 重建注入器 |
| ``hotwords`` | ✅ | 下一次开会话就带上 |
| ``asr.*``（地址/语言/上下文/分段时长） | ✅ | 地址变了要重连识别服务 |
| ``audio.*`（设备/块长/增益） | ✅ | 下一次开麦时生效 |
| ``hotkey.*`` | ❌ | 热键线程要重建，会打断正在进行的输入 |
| ``overlay.*``（宽度/透明度/字号） | ❌ | 悬浮窗是 Tk 窗口，重建会闪 |

「不能热更新」的部分**必须在界面上写清楚**，否则用户改完发现没反应，
会以为坏了 —— 这是 BF-13.3 要解决的事。

## 「保留一次连接预热」是什么意思

``LlmClient`` 用 ``urllib`` 发请求，**没有长连接**，所以没什么连接池可保。
真正要保的是：**重建客户端时，正在飞行中的请求不能被打断**。
所以新客户端建好之后才把旧的换掉（而不是先置空再新建），
换的过程是原子的 —— 正在跑的请求继续用旧客户端跑完。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

#: 可以热更新的配置段
HOT_SECTIONS = ("llm", "translate", "inject", "hotwords", "asr", "audio")

#: 需要重启才能生效的配置段
COLD_SECTIONS = ("hotkey", "overlay")


def diff_sections(old: dict, new: dict) -> set[str]:
    """比较两份配置，返回**变了**的段名。

    只看顶层段：段内部的字段差异由调用方自己判断（每个段的重建成本不一样，
    比如 ``asr`` 变了也不一定需要重连）。
    """
    changed: set[str] = set()
    for key in set(old or {}) | set(new or {}):
        if (old or {}).get(key) != (new or {}).get(key):
            changed.add(key)
    return changed


class ConfigWatcher:
    """盯着配置文件，变了就回调。

    用**轮询 mtime** 而不是 inotify/ReadDirectoryChangesW ——
    Windows 上那套要 ctypes 或额外依赖，而配置文件本来就小、
    改配置是低频操作，2 秒一次轮询完全够用，还少一堆平台分支。
    """

    def __init__(self, path: Optional[str | Path], on_change: Callable[[dict], None],
                 interval: float = 2.0) -> None:
        self.path = Path(path) if path else None
        self.on_change = on_change
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: 上一次**成功处理**过的内容指纹（sha256）。见 _load 的说明：
        #: 不用 (mtime, size) —— 那样会漏掉「等长内容」的改动。
        self._last_digest: Optional[str] = None
        #: 串行化 poll()（监视线程与外部调用可能同时进来，见 poll 的说明）
        self._lock = threading.Lock()
        #: 已经回调过几次（测试用）
        self.reload_count = 0

    # -- 生命周期 ---------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None or self.path is None:
            return
        # 记下**当前**内容的指纹 —— 启动前就已经存在的配置不算「变化」，
        # 不该触发回调（否则客户端一启动就会拿配置重载一遍）。
        _cfg, digest = self._load()
        self._last_digest = digest
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="config-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- 内部 -------------------------------------------------------------- #

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - 监视器绝不能拖垮主程序
                pass

    def poll(self) -> bool:
        """检查一次；有变化就加载并回调。返回是否触发了回调。

        单独拆出来是为了能**同步**调用（测试里不等 2 秒）。

        ⚠️ 整个过程要在**一把锁**里完成，而且 ``_last_digest`` 只能在
        **成功加载并回调之后**才更新。两个原因：

        1. **竞态**：监视线程和外部调用可能同时进来。不加锁的话，
           一个线程更新了 ``_last_digest`` 但回调还没跑，另一个线程就
           看到「没变化」直接返回 —— 回调于是被**吞掉**（实测：改成
           「已处理」但回调一次都没发生）。
        2. **不能因为一次读失败就丢掉这次改动**：设置界面是
           ``write_text`` 写文件 —— 先截断、再写。监视线程正好落在这个
           窗口里时，读到的是**半个文件**（JSON 解析失败）。
           如果这时就把 ``_last_digest`` 更新掉，这次改动就**永远丢了**，
           用户改了设置却没生效，而且再也等不到。
           所以：**读失败就不更新 digest**，下一轮重试。
        """
        if self.path is None:
            return False
        with self._lock:
            cfg, digest = self._load()
            if cfg is None or digest is None:
                # 读不到/读坏了 → **不更新 digest**，下一轮再试
                return False
            if digest == self._last_digest:
                return False
            self._last_digest = digest
            self.reload_count += 1
            try:
                self.on_change(cfg)
            except Exception:  # noqa: BLE001 - 回调失败不该杀掉监视线程
                pass
            return True

    def _load(self) -> tuple[Optional[dict], Optional[str]]:
        """读配置。**坏文件不能让监视线程死掉** —— 读到 None 就跳过这一轮。

        返回 ``(配置, 内容指纹)``。指纹是**内容的哈希**。

        这里不直接用 ``config.load_config``：那个函数在文件损坏时会
        **回退默认值**，那样会把用户刚改好的设置**整个冲掉**。
        这里宁可跳过，也不覆盖。

        ## 为什么指纹用内容哈希，而不是 (mtime, size)

        以前用 ``(mtime, size)`` 判断「文件变没变」。这有两个真实的漏检：

        1. **等长的内容改不动**：``{"llm": {"model": "m1"}}`` 和
           ``{"llm": {"model": "m2"}}`` **字节数完全一样**（24 字节）。
           如果文件系统的 mtime 粒度粗（CI runner 上实测会这样），
           两次写入的 mtime 可能相同 → 改动被**静默丢弃**，
           用户改了设置却没生效。
        2. **mtime 粒度**：某些文件系统只精确到秒。同一秒内的两次写入，
           mtime 一样 → 后一次被当成「没变」。

        改成内容哈希就没有这些问题：**内容一样才算没变**，
        字节数一样但内容不同，一定能认出来。

        哈希用 sha256 —— 配置文件才几 KB，2 秒一次完全不是负担。
        """
        try:
            raw = self.path.read_bytes()
        except OSError:
            return None, None
        digest = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        return data, digest


def describe_effects() -> dict[str, str]:
    """给设置界面用的说明文案：哪一段热更新、哪一段要重启。"""
    return {
        "llm": "立即生效（修正/翻译参数会在下一次说话时用上）",
        "translate": "立即生效（下一次说话时用上）",
        "inject": "立即生效（下一次注入时用上）",
        "hotwords": "立即生效（下一次说话时带给识别引擎）",
        "asr": "立即生效（地址变了会重连识别服务）",
        "audio": "立即生效（下一次开麦时用上）",
        "hotkey": "需要重启客户端",
        "overlay": "需要重启客户端",
    }
