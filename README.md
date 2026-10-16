# wx-cli · 微信 Agent 渠道

让现有 Agent 通过 CLI 按需读取授权的群聊或个人会话、确认需求并回复；已验证的自动运行基线仍是单个绑定群。**结构化读取 + 持久消费进度 + 一次性发送 + 会话需求笔记**由共享 channel/outbox 契约连接起来。

本仓库基于 [pandorafuture/wx-cli](https://github.com/pandorafuture/wx-cli)，保留上游历史与 MIT 许可证。新增的群/个人会话 CLI 当前是按需手动入口；其真实桌面与手机验证仍待完成。有限的既有单群测试不代表客户环境、媒体发送、多常驻 Agent 或全天无人值守已获验证。

[完整操作指南](docs/OPERATING_GUIDE.md) · [群/个人会话 CLI](docs/CONVERSATIONS_CLI.md) · [单群开始使用](docs/quickstart.md) · [架构](docs/architecture.md) · [会话能力图](docs/diagrams/conversation-capabilities.html) · [消息契约](docs/contracts.md) · [验证范围](docs/REAL_READ_VALIDATION_20261015.md) · [安全边界](SECURITY.md)

## 一个仓库，三个执行层

| 层 | 提供什么 | 入口 |
| --- | --- | --- |
| Rust / wx-cli | 加密副本读取、稳定群/个人会话身份发现、NDJSON 事件、consumer 确认、持久发件箱 | `wx-cli conversations` / `wx-cli channel` / `wx-cli outbox` |
| Python / wx-agent | 单群自动运行、按需会话选择/分页/聚焦/发送/回复、启停与健康状态、Codex 适配、带来源的会话笔记 | `wx-agent` |
| Swift / macOS bridge | 核验目标窗口与草稿，准备文本，单次点击发送 | 独立构建和授权的原生助手 |

项目内 [wechat-group-agent skill](.agents/skills/wechat-group-agent/SKILL.md) 教 Agent 如何操作上述 CLI。skill 不自带后台监听器，也不会为手动会话启动模型；MCP 尚未实现，后续若开发应复用同一套状态和协议。

当前 Agent 可通过 [`wx-agent conversations`](docs/CONVERSATIONS_CLI.md) 按需发现并选择授权的群聊或个人会话、分页阅读、聚焦、发送或回复；纯手动流程无需模型或常驻 Agent。若选择的群复用已有单群 controller，桌面写操作会协调该原 writer，并在完成后恢复原启停状态。`wx-agent chat` 仍是既有单群入口。多常驻 Agent、逐会话运行模式表、MCP 与共享桌面多群调度尚未实现。

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

通用 `wx-agent conversations` 的新增路径尚未完成真实 Mac/微信现场验证。现有单群实测不得延伸成个人会话发送、多会话桌面切换或新功能已送达的证据；操作范围和待验证项见[会话 CLI 指南](docs/CONVERSATIONS_CLI.md)。

## 开发

使用 [贡献指南](CONTRIBUTING.md) 运行 Rust、Python、原生参数和安装包检查。私有状态只放忽略目录；公开检查是辅助筛查，不代替人工审阅。[ADR 0001](docs/adr/0001-unified-repository.md) 解释为何采用统一仓库，而不是 submodule。

上游通用查询、搜索和媒体解析仍保留，使用 `wx-cli --help` 发现；其上游能力范围不等同本 fork 的群聊自动回复验收范围。以 [MIT](LICENSE) 分发。
