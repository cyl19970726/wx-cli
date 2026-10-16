# 一个群、一套事件、一个发送者

本项目把微信作为 Agent 的一个渠道：从本机加密数据库副本获取结构化消息，以持久事件游标订阅；Agent 生成回复后，经一次性发件箱与 macOS 桌面发送。读取正文不依赖截图 OCR。视觉识别主要用于核验目标窗口、群标题、输入框和发送按钮。

## 包与责任

```text
wx-cli/
├── crates/                         Rust workspace
│   ├── wx-cli/src/cmd/channel*      群绑定、加密副本、事件、订阅与确认
│   ├── wx-cli/src/cmd/outgoing.rs   发件箱、尝试记录、驱动协议
│   └── wx-{db,context,decrypt,...}  上游数据库、密钥与解析能力
├── packages/wx-agent/               Python 安装包
│   └── src/wx_agent/
│       ├── cli.py                  wx-agent 统一入口
│       ├── channel_agent.py        入站 → 模型 → 发件箱 → 确认
│       ├── wechat_agent_ctl.py      生命周期与进程身份校验
│       ├── codex_reply_agent.py     已登录 Codex 的文本生成适配
│       ├── memory.py                带来源和版本的会话需求笔记
│       ├── macos_sender_driver.py  图形助手适配与本地出站核对
│       └── sender_guard_probe.py   有界保护条件核验
├── native/macos/wechat-bridge/      Swift：窗口、草稿、剪贴板、单次点击
├── scripts/                         兼容入口、集成测试与仓库检查
├── .agents/skills/wechat-group-agent/项目内操作 skill
├── examples/                        仅合成配置
└── docs/                            用法、契约、决策与验证范围
```

## 数据与控制

| 数据 | 唯一负责者 | 生命周期 |
| --- | --- | --- |
| 微信原库与密钥 | 微信 / 本机 KeyStore | 不提交仓库；原库只按字节复制 |
| 临时加密副本 | Rust snapshot | 一轮采集使用，正常退出清理 |
| channel 绑定与事件、consumer cursor | Rust channel | 群绑定不变；增量持久化，显式 ack |
| outgoing intent / attempt | Rust outbox | 请求 ID 幂等；尝试后不盲重发 |
| 当前待回复正文、健康与控制状态 | Python runner/controller | 重启续接；错误停发，状态日志不含正文 |
| 会话需求笔记 | Python memory | 派生上下文，版本检查，保留入站来源；不能改消息原文 |
| 图形准备与提交标记 | Swift helper | 单个请求只提交一次，结果未知保留证据 |

读取事件不会自动确认消费。运行器仅处理绑定群的明确入站文本；自己的出站、更新事件及非文本不会触发自我对话。准备好的回复先落盘，再进入 outbox。达到本地 `ui_observed` 才确认事件；不明结果保留 pending 并停止，重启也不会换 ID 重发。

控制器与执行器分开：`wx-agent start` 启动本机运行器，`status/pause/resume/stop` 控制它。暂停和停止在安全边界生效，不强杀正在提交的发送。Mac mini 可以同时运行读取、模型适配和发送，不依赖控制笔记本的 SSH 会话。

## 会话记忆

持久事件日志保存可查询的原文，模型的短上下文保留最近 12 条有限长度文本；需求笔记提供更早的重要事项。笔记由操作者或接入 Agent 通过明确 CLI 更新，分为摘要、已确认事项和未决问题，指向同一群内已存在的入站游标。比较 revision 防止覆盖他人的更新。

来源校验只证明该消息存在且属于当前群，不证明笔记的解释必然正确。当前不会让回复模型自动写“已确认需求”，也不会用笔记代替原文。旧笔记与新入站矛盾时需要追问或修订。缺少笔记保持原有行为；有笔记但损坏、绑定错误时拒绝继续生成。

## 适配边界

Agent 通过 JSON stdin/stdout 调用，可替换 Codex 文本适配器。该适配器禁用 shell、应用和联网工具，只产文案。群消息没有授权本机操作的能力。CLI 才拥有群绑定和状态；skill 解释何时调用、如何处理人工接管和确认需求。

MCP 后续暴露相同契约，不拥有第二份消费进度；多群需额外实现共享桌面调度、全局发送锁及身份切换验证，不能简单复制多个自动回复循环。

## 现场与开发版

已部署的旧版完成过单群有限实测。新包、会话笔记和安装流程有自己的离线验证；未经明确升级和现场回归，不把旧证据写成新版本已部署。桌面锁定、微信退出、权限失效或无法完整读取剪贴板时，发送会停止。当前助手的输出目录仍有部署约束，详见 [原生助手](../native/macos/wechat-bridge/README.md)。

[仓库决策](adr/0001-unified-repository.md) · [消息契约](contracts.md) · [单群用法](SINGLE_GROUP_CLI.md) · [验证记录](REAL_READ_VALIDATION_20261015.md)
