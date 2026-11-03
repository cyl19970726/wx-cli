# wx-cli · 微信 Agent 渠道

让现有 Agent 通过 CLI 按需读取授权的群聊或个人会话、确认需求并回复；已验证的自动运行基线仍是单个绑定群。**结构化读取 + 持久消费进度 + 一次性发送 + 会话需求笔记**由共享 channel/outbox 契约连接起来。

本仓库基于 [pandorafuture/wx-cli](https://github.com/pandorafuture/wx-cli)，保留上游历史与 MIT 许可证。CLI 已支持群聊和个人会话的选择、读取与手动回复；单群接管和交还后台已实测。2026-10-20 的 `d8f4910` 是已确认的历史基线；10 月 21 日统一修复版 `de15fde` 也已完成授权私聊、测试群发送及自动切回，两条新消息均由独立收件端确认各收到一次。群聊和私聊端到端发送分别约 14.4 秒、9.2 秒；读取约 0.28 秒。终止换行保护、空框校准、精确发送按钮核验与历史发件记录保留均有相应证据，见[验收记录](docs/CONVERSATIONS_ACCEPTANCE_20261020.md)。长期无人值守、其他客户环境和多常驻 Agent 尚未验证。

[远端开发与本地验收](docs/REMOTE_DEVELOPMENT.md) · [完整操作指南](docs/OPERATING_GUIDE.md) · [群/个人会话 CLI](docs/CONVERSATIONS_CLI.md) · [会话验证状态](docs/CONVERSATIONS_VALIDATION_20261016.md) · [单群开始使用](docs/quickstart.md) · [架构](docs/architecture.md) · [会话能力图](docs/diagrams/conversation-capabilities.html) · [消息契约](docs/contracts.md) · [既有验证范围](docs/REAL_READ_VALIDATION_20261015.md) · [安全边界](SECURITY.md)

## 一个仓库，三个执行层

多会话扩展的设计见[批准方案](docs/MULTI_CONVERSATION_PLAN.md)，新增入口见[托管会话指南](docs/MANAGED_CONVERSATIONS.md)，实际进度见[实施与验证记录](docs/MULTI_CONVERSATION_IMPLEMENTATION.md)。本分支增加逐会话控制、Rust 持久 inbox/文字图片调度与受管 Codex Session 适配器；新版本的工程验证、实机发送和独立收件分别记录，不扩大下述历史验收范围。

| 层 | 提供什么 | 入口 |
| --- | --- | --- |
| Rust / wx-cli | 加密副本读取、稳定身份、事件/consumer/outbox，以及托管 owner、inbox 与调度引用 | `wx-cli conversations` / `channel` / `outbox` / `coordinator` |
| Python / wx-agent | 按需 CLI、旧单群兼容、托管采集/Session 分发、独立桌面执行器及派生健康状态 | `wx-agent` |
| Swift / macOS bridge | 核验目标窗口与草稿，准备文本或单张 PNG/JPEG，单次点击发送 | 独立构建和授权的原生助手 |

项目内 [wechat-group-agent skill](.agents/skills/wechat-group-agent/SKILL.md) 教 Agent 如何操作上述 CLI。skill 不自带后台监听器，也不会为手动会话启动模型；MCP 尚未实现，后续若开发应复用同一套状态和协议。

通道能力与 Agent Session 接入分开：wx-cli 保存事件、订阅进度和发送队列，普通应用可直接提交授权的群通知，Session 插件负责通知原 Session 并消费事件。当前候选增加[应用客户端与鉴权网关](docs/APPLICATION_CLIENT.md)及[项目级 Codex 插件](docs/SESSION_PLUGIN.md)，复用原 inbox/outbox。两个应用的鉴权异机发送、限定范围的异机只读 inbox API，以及原专用 native TUI Session 的单回合入站处理已分别实测；桌面 App 持有的 Session、异机 Session 插件和真实图片队列仍待验收。架构与证据边界见[通道订阅、应用发件与 Session 插件](docs/CHANNEL_SUBSCRIPTIONS.md)和[实施记录](docs/MULTI_CONVERSATION_IMPLEMENTATION.md)。

Agent 可通过 [`wx-agent conversations`](docs/CONVERSATIONS_CLI.md) 按需发现并选择授权的群聊或个人会话、分页阅读、聚焦、发送或回复；纯手动流程无需模型或常驻 Agent。未迁移会话继续使用原 controller 协调；托管会话通过 `configure` 显式迁移，使用 `wx-agent inbox` 和 `dispatch` 共享处理记录及桌面队列。原专用 native TUI Session 已有受管入站闭环；正在被桌面 App 持有的业务 Session 暂只支持主动 CLI。MCP 尚未实现。

图片发送命令、内容冻结与当前实机状态见[单图片发送记录](docs/IMAGE_SEND_VALIDATION_20261024.md)和[CLI 用法](docs/CONVERSATIONS_CLI.md)。

## 已经具备的能力

- **消息识别**：正文来自本地加密数据库副本，带发送者、群身份、方向、消息 ID 和游标；不靠 OCR 拼接聊天正文。
- **按需会话**：通过联系人/群元数据发现稳定会话 ID，为群或个人会话分别保存本地状态；重名候选会全部呈现，桌面操作要求名称唯一。
- **增量处理**：事件保存后显式确认，退出可以续读；区分同文不同消息、更新事件及本账号出站。
- **回复控制**：生成的文案先保存，再入发件箱；发送尝试已记录或结果不明时停止，避免盲目重发。
- **需求记忆**：摘要、已确认事项、未决问题独立保存，引用当前群的入站消息；revision 检查避免互相覆盖。由操作者或接入 Agent 明确更新，不自动把模型推测确认为事实。
- **运行管理**：启动、状态、暂停、恢复、停止；可在操作端独立运行，不依赖控制电脑维持 SSH。

## 安装与入口

Rust workspace 与 Python 包各自构建，发布版本不等同部署版本。macOS 图形助手另行构建和授权，Python 安装不会替换它。

```sh
cargo build --release --locked
python3 -m venv .local/venv
.local/venv/bin/python -m pip install ./packages/wx-agent

./target/release/wx-cli channel --help
.local/venv/bin/wx-agent --help
```

需要 macOS / 已登录微信 / 本地密钥以及已校准的图形助手。依赖和初始化步骤见 [开始使用](docs/quickstart.md)。仓库不包含账号、密钥、桌面配置、消息记录或已签名应用。

## 验证到哪里

结构调整前的单群链路完成过真实读取、发送、跨重启上下文和一次 **32 分钟**的有界观察；读取入口恢复后补回一次积压，连续两条入站按序回复，重启未重发。部分旧消息有独立收件确认；新四条自动回复仅验证到本地出站。

0.2.0 已在授权测试端隔离部署，实测选群、分页、按需读取、人工回复与交还后台；本轮两条回复有用户提供的收件端截图和确认。会话笔记与故障分支另有合成数据验证。窗口锁定、微信退出、权限变化或不可读剪贴板仍可能停止图形发送。最新证据见 [交互 CLI 验证](docs/INTERACTIVE_VALIDATION_20261016.md)，早期证据见 [读取与发送验证](docs/REAL_READ_VALIDATION_20261015.md)。

通用 `wx-agent conversations` 的已完成基线、后续候选和现场安装状态分别记录在[验收记录](docs/CONVERSATIONS_ACCEPTANCE_20261020.md)中。失败或不确定意图保留原记录，不因升级重发；单次有界往返不代表长期稳定性。

## 开发

使用 [贡献指南](CONTRIBUTING.md) 运行 Rust、Python、原生参数和安装包检查。私有状态只放忽略目录；公开检查是辅助筛查，不代替人工审阅。[ADR 0001](docs/adr/0001-unified-repository.md) 解释为何采用统一仓库，而不是 submodule。

上游通用查询、搜索和媒体解析仍保留，使用 `wx-cli --help` 发现；其上游能力范围不等同本 fork 的群聊自动回复验收范围。以 [MIT](LICENSE) 分发。
