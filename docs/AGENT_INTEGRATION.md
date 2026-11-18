# Agent 与指定微信群或私聊

采用 **项目内 skill + CLI**。CLI 是基础访问能力，常驻 Agent 是可选调用方。用户可以按需查看一个群的历史、让当前 Agent 处理一段聊天，也可以为群配置长期运行的 Agent；读取消息不等于必须回复。

| 使用方式 | 操作 | 是否需要后台 Agent |
| --- | --- | --- |
| 按需访问历史 | find → select → read，按游标继续翻页 | 不需要 |
| 当前 Agent 处理聊天 | read → 判断后 reply，或不回复 | 不需要另一个回复模型 |
| 长期 Session | 托管会话：持久 inbox → 通知原 Session → 记录 skip/defer/draft/reply | 需要采集、通知与共享桌面执行器 |

入口是 [`wx-agent conversations`](CONVERSATIONS_CLI.md)；托管会话的 `takeover` / `handback` 是人工与 Session 之间的协调机制，不是查看历史的必经流程。多个只读调用可以查询同一日志，但同一会话只保留一个负责推进处理和发送的 owner。旧的单群 runner 已移除。

- [wechat-group-agent skill](../.agents/skills/wechat-group-agent/SKILL.md)：已作为仓库文件提供，不安装全局。
- [wx-agent](../packages/wx-agent/pyproject.toml)：Python 安装包，会话 CLI、托管 inbox/dispatch、Session 适配、会话笔记。
- [channel CLI](SINGLE_GROUP_CLI.md)：底层绑定、订阅和发件箱契约。
- [契约](contracts.md)：版本、身份、来源和不明发送结果。

会话原文由 Rust channel 保留；模型最近上下文之外，笔记保存有来源的摘要、已确认事项和未决问题。笔记由操作者或接入 Agent 显式更新，回复模型不自行写回。新功能的离线通过与旧版真实实测分开记录。

MCP 尚未实现。未来只增加相同命令/协议的工具适配层，不增加第二套消息进度、记忆或发送重试。托管会话使用逐会话 owner/epoch、Rust inbox 和共享文字/图片调度。新会话默认 observe，不自动回复每条消息。步骤见[托管指南](MANAGED_CONVERSATIONS.md)，实际证据见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)。

两种入口并存：专属 Session 主动使用 CLI，或增量消息投递给该 Session；共享处理租约、决定、持久待办及原文字/图片 outbox。Codex 适配器已在明确受管的同一测试 thread 上完成跨重启工具调用与合成 inbox 写回；桌面 App 正持有的业务 thread 继续主动 CLI，不由独立进程强行 resume。批准方案的目标与批次见[多会话接入方案](MULTI_CONVERSATION_PLAN.md)；此分支代码和离线通过不表示多会话现场或图片收件通过。

目标能力与当前范围见[会话 CLI 能力图](diagrams/conversation-capabilities.html)；可重建输入位于 [tree JSON](diagrams/conversation-capabilities.tree.json)。
