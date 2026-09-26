# 第三方组件与许可

本仓库**不包含**下列项目的任何代码或权重，它们只作为依赖或设计参考。
单独列在这里（而不是写在 `LICENSE` 里）是因为：把额外文字混进 `LICENSE`
会导致 GitHub 无法识别许可证类型，仓库首页只会显示 `NOASSERTION` 而不是 `MIT`。

## 运行依赖

| 组件 | 许可 | 说明 |
|---|---|---|
| [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2)（网易有道） | 代码 **Apache-2.0** | 识别引擎。本项目通过 `import` 调用用户自行部署的服务，**未复制其代码** |
| **Confucius4-R2T2 模型权重** | **NetEase Model Use License** | 与代码许可**不同**。本仓库不分发权重，商用前请自行确认条款 |
| [sounddevice](https://python-sounddevice.readthedocs.io/) | MIT | 音频采集（内含 PortAudio，MIT） |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | WebSocket 客户端 |
| [NumPy](https://numpy.org/) | BSD-3-Clause | 数值计算 |
| Python / Tcl-Tk | PSF / Tcl-Tk License | 打包产物内含 |

## 设计参考（**未复制任何代码**）

调研记录见 `docs/prior-art/`。

| 项目 | 许可 | 借鉴了什么 |
|---|---|---|
| [Typeflux](https://github.com/mylxsw/typeflux) | **AGPL-3.0** | 按住说话 + 流式预览的交互思路。**AGPL 代码不可复制**，本项目一行未取 |
| [CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline) | MIT | LLM 后处理的**失败分类**思路（认证/连接 → 提示；超时/限流 → 静默降级） |
| [openwhispr](https://github.com/OpenWhispr/openwhispr) | MIT | 输出校验（复读检测）、"末尾指令权重最高"的 prompt 编排 |
| [VoiceX](https://github.com/xuyungit/VoiceX) | MIT | 剪贴板还原规则（内容未变才还原）、按目标应用区分注入的设计 |
| [push-2-talk](https://github.com/yyyzl/push-2-talk) | MIT | Windows 上优先用 `GetAsyncKeyState` 轮询而非低级钩子的判断 |
| [opentypeless](https://github.com/tover0314-w/opentypeless) | MIT | prompt 分节的优先级组织方式 |

> 上述项目的具体发现与出处见 `docs/prior-art/` 下的两份调研报告。
