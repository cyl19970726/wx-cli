# 微信通道独立，Session 通过插件接入

wx-cli / 通道服务负责授权微信会话的采集、订阅、持久 inbox 与共享发送队列。普通应用可直接提交发送意图；Session 插件消费订阅、通知原 Agent Session，并把明确处理决定或发送意图交回同一通道。微信通道不负责创建、恢复或持有模型 Session。这是 2026-11-03 用户澄清后的架构边界，合同与现有入口见[通道订阅、应用发件与 Session 插件](CHANNEL_SUBSCRIPTIONS.md)。

`channel subscribe` 独立输出事件，`coordinator inbox` 保存待办和处理决定。本项目级 Session 插件通过 queue-snapshot adapter 与鉴权 RPC 通知同一个原 App-held Session；这条路径不创建或恢复该 Session。面向自有宿主连接的 `integrations/codex.py` / `session_adapter.py` 托管适配仍独立存在，不在本次 Hook transport 退役范围内。实际范围见实施记录与[Session 插件指南](SESSION_PLUGIN.md)。

本分支的托管扩展使用 `coordinator.rs` 管理 owner/control epoch、通知水位、处理决定/待办、transport reservation 与发送引用；`dispatch.py`、`inbox.py` 负责确定性编排。`application_client.py` 为普通应用保留稳定请求命名空间，`gateway.py` 限定凭据与会话范围，`gateway_client.py` 可经 SSH 调用同一协议。共享 Session 路由由 `session_routes.py` 中的 `SessionBindings` 承载；`queued_snapshot.py` 将冻结事件派生为私有只读输入，并验证原宿主的完整读取、严格决定提案和成功终态后，通过同一网关记录决定。快照没有独立游标或发送权限。旧 `session_adapter.py` 只持有明确受管的长期 Codex 连接。通道的 owner 是业务处理身份，不是应用凭据或 Codex thread ID。运行方法见[应用接入](APPLICATION_CLIENT.md)、[Session 插件](SESSION_PLUGIN.md)及[托管指南](MANAGED_CONVERSATIONS.md)，实测见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)。以下历史单群/未托管描述继续适用于尚未迁移的会话。

本项目把微信作为 Agent 的一个渠道：从本机加密数据库副本获取结构化消息，以持久事件游标订阅；Agent 生成回复后，经一次性发件箱与 macOS 桌面发送。读取正文不依赖截图 OCR。视觉识别主要用于核验目标窗口、群标题、输入框和发送按钮。

按需入口 `wx-agent conversations` 同时支持群聊和个人会话。元数据发现返回由账号与原始 talker 派生的稳定 `conversation_id`，显示名变化不会换 ID。注册表只保存选择与路由；每个新会话仍使用一套不可变 Rust channel/outbox。已有 controller 必须在配置中显式列入，选择对应群时复用其原状态，不能创建第二个消费者来绕过正在运行的后台。完整入口见[通用会话 CLI](CONVERSATIONS_CLI.md)。

只读访问不启动模型或后台；发送和聚焦会临时协调登记的 controller，并由共用 UI 锁串行操作桌面。新手动会话的回复引用真实入站事件并使用稳定请求 ID，读取与回复不自动确认 consumer；导入旧群时则沿用旧交互回复的顺序、确认和交还语义。

界面导航依赖标题，无法读取微信原生当前会话 ID。操作前从加密元数据副本核对稳定 ID、名称与类型，拒绝重名候选；图形助手按 Contacts、Group Chats、Most used 分类搜索，并用 120 秒内的新鲜搜索证据绑定 focus，在各视觉阶段核对标题和类型、群成员数。发送后核对精确 talker 的本地出站事件；本地 `ui_observed` 不等同收件端确认。2026-10-20 基线和 10 月 21 日统一修复版都完成过授权测试身份内的有界私聊／群聊发送、自动切换及独立收件确认；历史不确定请求继续保留，不能重放。长期稳定性未验证，详细状态见[日期验收记录](CONVERSATIONS_ACCEPTANCE_20261020.md)。桌面操作要求专用会话且操作期间无人手动切换；相同渲染标题仍有 TOCTOU 风险，因此目标核验不是官方身份 API、绝对保证或生产级隔离。

[打开交互原理图](diagrams/agent-channel.html) · [图源与重建](../.archify/README.md)

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
│       ├── interactive.py          选群、分页、接管与交还编排
│       ├── conversations.py        群/私聊目录、独立绑定、选择与按需操作
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

outbox 的未知结果不自动重试。窄范围 `recover-unsent` 只允许操作者按原 request ID 和当前 attempt 显式请求恢复；Rust 先要求驱动能力握手及只读 preflight，再以事务 CAS 领取下一次不可变 attempt。驱动在共享 UI 锁内重新校验原始准备失败、完整匹配的私有作业/结果、权限证据、无 submit 记录及无存活 helper 后写入恢复证明，并且只执行一次尝试。缺少任一证据都停止，不回退到普通发送、不换 ID。可恢复形态由契约严格限定为准备阶段的剪贴板/草稿失败，以及有完整历史证明的更早 inspect/focus 停止；超时本身不是未发送证明，真实提交或错误目标发送不属于此机制。详细规则见[发件箱恢复契约](contracts.md#显式恢复已证实未提交的尝试)。

读取事件不会自动确认消费。运行器仅处理绑定群的明确入站文本；自己的出站、更新事件及非文本不会触发自我对话。准备好的回复先落盘，再进入 outbox。达到本地 `ui_observed` 才确认事件；不明结果保留 pending 并停止，重启也不会换 ID 重发。

控制器与执行器分开：`wx-agent start` 启动本机运行器，`status/pause/resume/stop` 控制它。暂停和停止在安全边界生效，不强杀正在提交的发送。Mac mini 可以同时运行读取、模型适配和发送，不依赖控制笔记本的 SSH 会话。

交互 Agent 使用 `wx-agent chat`，直接读取结构化事件并撰写文案；它与常驻运行器共用同一 Rust outbox。接管通过等待后台退出、检查 pending、取得运行锁与持久占用标记完成。手动回复按源消息生成相同请求 ID，并比较原 consumer 游标后确认。交还恢复先前运行状态；新进程完成就绪握手后才解除占用。图中 Agent 运行器节点概括此编排层，交互命令细节见 [交互 CLI](INTERACTIVE_CLI.md)。

## 会话记忆

持久事件日志保存可查询的原文，模型的短上下文保留最近 12 条有限长度文本；需求笔记提供更早的重要事项。笔记由操作者或接入 Agent 通过明确 CLI 更新，分为摘要、已确认事项和未决问题，指向同一群内已存在的入站游标。比较 revision 防止覆盖他人的更新。

来源校验只证明该消息存在且属于当前群，不证明笔记的解释必然正确。当前不会让回复模型自动写“已确认需求”，也不会用笔记代替原文。旧笔记与新入站矛盾时需要追问或修订。缺少笔记保持原有行为；有笔记但损坏、绑定错误时拒绝继续生成。

## 适配边界

Agent 通过 JSON stdin/stdout 调用，可替换 Codex 文本适配器。该适配器禁用 shell、应用和联网工具，只产文案。群消息没有授权本机操作的能力。CLI 才拥有群绑定和状态；skill 解释何时调用、如何处理人工接管和确认需求。

MCP 后续暴露相同契约，不拥有第二份消费进度。托管扩展通过 Rust coordinator 和独立桌面执行器协调多个会话，使用显式模式和 owner；不复制自动回复循环。跨宿主 App-held Session 接入由项目 Skill、插件 bundle、queue-snapshot adapter 和受限网关提供；桌面 App 持有的 thread 只经原宿主队列通知，不创建第二个 writer。面向自有宿主连接的托管适配是独立路径。自动冷唤醒未实现，跨账号切换未接入。

## 现场与开发版

已部署的旧版完成过单群有限实测。新包、会话笔记和安装流程有自己的离线验证；未经明确升级和现场回归，不把旧证据写成新版本已部署。桌面锁定、微信退出、权限失效或无法完整读取剪贴板时，发送会停止。当前助手的输出目录仍有部署约束，详见 [原生助手](../native/macos/wechat-bridge/README.md)。

[仓库决策](adr/0001-unified-repository.md) · [消息契约](contracts.md) · [单群用法](SINGLE_GROUP_CLI.md) · [验证记录](REAL_READ_VALIDATION_20261015.md)
