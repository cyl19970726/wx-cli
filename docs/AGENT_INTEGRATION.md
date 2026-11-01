# Agent 与指定微信群

采用 **项目内 skill + CLI**。CLI 是基础访问能力，常驻 Agent 是可选调用方。用户可以按需查看一个群的历史、让当前 Agent 处理一段聊天，也可以为群配置长期运行的 Agent；读取消息不等于必须回复。

| 使用方式 | 操作 | 是否需要后台 Agent |
| --- | --- | --- |
| 按需访问历史 | groups → select → read，按游标继续翻页 | 不需要；不启动、暂停或接管后台 |
| 当前 Agent 处理聊天 | 取得写入权 → read → 选择 reply 或 skip → 释放写入权 | 不需要另一个回复模型；已有后台时才涉及临时接管 |
| 常驻 Agent | 显式启动运行器，持续读取并判断 reply / skip | 需要；按该群绑定和消费进度持续运行 |

交互入口使用 [`wx-agent chat`](INTERACTIVE_CLI.md)。`takeover` / `handback` 是发言者之间的协调机制，不是查看历史的必经流程。接管等待原运行器安全退出，交还恢复原启停状态；原先没有运行的后台不会因交还而启动。多个只读调用可以查询同一日志，但同一绑定只保留一个负责推进消费和发送的调用方。

- [wechat-group-agent skill](../.agents/skills/wechat-group-agent/SKILL.md)：已作为仓库文件提供，不安装全局。
- [wx-agent](../packages/wx-agent/pyproject.toml)：Python 安装包，生命周期、Codex 适配、会话笔记。
- [单群 CLI](SINGLE_GROUP_CLI.md)：绑定、订阅、发件箱和状态控制。
- [契约](contracts.md)：版本、身份、来源和不明发送结果。

会话原文由 Rust channel 保留；模型最近上下文之外，笔记保存有来源的摘要、已确认事项和未决问题。笔记由操作者或接入 Agent 显式更新，回复模型不自行写回。新功能的离线通过与旧版真实实测分开记录。

MCP 尚未实现。未来只增加相同命令/协议的工具适配层，不增加第二套消息进度、记忆或发送重试。当前 `wx-agent conversations` 已有群／私聊注册表和独立 channel 状态，支持按需访问；已验证的常驻运行范围仍为单群。逐会话模式／专属 Session 绑定、消息推送和持久全局发送队列尚未实现。

新增需求采用两种触发入口：专属 Session 主动使用 CLI，或增量消息投递给该 Session；两者共用读取、处理进度、文字／图片 outbox 和桌面发送通道。完整目标、现状差距、图片候选验收边界与实施批次见[多会话接入方案](MULTI_CONVERSATION_PLAN.md)。该方案是待实现设计，不代表多群常驻或图片实机验收已经通过。

目标能力与当前范围见[会话 CLI 能力图](diagrams/conversation-capabilities.html)；可重建输入位于 [tree JSON](diagrams/conversation-capabilities.tree.json)。
