# 渠道、Agent 与发送契约

这些是当前实现的进程协议，不是微信官方 API。未来 MCP 必须复用这些语义。

## 身份与进度

- `channel_id`：本地固定频道身份。一个状态目录只绑定一个账号与群或个人会话。
- `conversation_id`：通用目录身份，由账号与原始 talker 派生，与显示名无关；每个注册项引用独立的 channel 状态，不能通过切换选择重绑旧目录。
- `session_id`：调用者明确指定的 Agent 会话标签。不是微信会话 token。
- `consumer`：独立读取方的确认位置。多个读者可以独立消费；当前运维只允许一个自动发言者。
- `cursor`：本地事件日志位置，不能当作微信消息时间或官方消息 ID。
- `server_id`：源消息身份。`message_observed` 与 `message_updated` 是不同事件类型；同文不同 ID 不去重成一条。

`channel status` 返回 binding、latest_cursor 和覆盖范围。`events`/`subscribe` 返回 NDJSON，包含上述身份、`account`、`talker`、direction、message；coverage 为 `bounded_local_database`，history_complete 为 false。不将“本机没有”解释为“微信从未存在”。

`channel status --consumer NAME` 额外返回该 consumer 的持久 `consumer_cursor`。`ack --expected-cursor N` 在同一事务内比较原位置，不匹配时拒绝推进；无此参数的旧接口保持兼容。交互入口必须使用比较式确认，不能跳过更早未处理的入站文本。

`wx-agent chat` 的选择记录、交互占用和手动 pending 是私有编排状态；事件与发送的权威记录仍由 Rust 管理。接管不换 consumer；未知发送保留原请求。交还只恢复接管前的启停状态，不把已经停止的运行器自动开启。命令与错误处理见 [交互 CLI](INTERACTIVE_CLI.md)。

`wx-agent conversations` 的私有注册表保存会话选择与原 controller 路径，消息和 outbox 仍仅由 Rust 持久化。`read` 返回调用方可保存的 next_cursor，不自动 ack；新手动会话 `reply` 使用来源消息的固定请求 ID 防重，也不自动 ack。导入 controller 的回复复用原顺序和 CAS 确认。`send` 的请求 ID 在各会话 outbox 内有效；通用 sender 的原生事务 ID 同时包含 channel_id，避免跨会话同名请求冲突。所有 GUI 操作会检查全候选中的标题混淆、原状态绑定和共用 UI 锁。

## Agent 请求

运行器向明确指定的命令 stdin 发送单个 JSON：

```json
{"version":1,"event":{},"recent_context":[],"session_memory":{}}
```

`event` 是当前真实事件；recent_context 最多 12 条有限文本；session_memory 仅在存在有效私有笔记时提供。占位对象不能直接用于运行。输出必须为 `{"text":"..."}` 或 `{"skip":true}`。文本先持久化，再交给 outbox。模型调用失败有限重试，发送不做相同重试。

Codex 适配器匿名化成员标签，并只把笔记文案加入提示，不把账号、群 ID、路径或来源游标传入模型提示。聊天内容没有工具执行授权。更换 Agent 命令属于运行配置变化，旧状态会拒绝隐式漂移。

## 发件箱

`queue` 接受 request_id/channel_id/session_id 与 stdin 文本。同请求同正文幂等；同 ID 不同正文拒绝。`send` 先持久化 attempt，再启动驱动。

| 状态 | 含义 | 自动处理 |
| --- | --- | --- |
| queued | 已保存，未尝试 | 可进入一次发送 |
| attempt_recorded | 已记录尝试，结果未保存 | 不重放 |
| ui_observed | 本地观察到匹配出站 | 可以确认消费；不是收件回执 |
| recipient_confirmed | 有另外记录的收件证据 | 不重放 |
| failed / indeterminate | 失败或不明 | 停止；核对原请求 |

驱动协议 `protocol_version:1`，stdin 包含 request_id、channel_id、session_id、account、talker、text。stdout 返回 status 与非空 evidence。退出码 0 不是送达证明，调用者必须检查 status。运行器不会用替换 ID 绕过不明状态。

## 会话笔记

`wx-agent memory show` 返回版本、绑定、revision、summary、summary_source_cursors、confirmed 和 open_questions。每个事项是 `{text, source_cursors}`。不存在文件时 revision 为 0。

`update --expected-revision N --file PRIVATE_JSON` 原子替换整份笔记。更新输入不包含 revision；版本冲突时重新读取并合并，不强行覆盖。非空摘要和每个事项必须引用同一绑定群内已存在的 `message_observed/incoming` 游标，拒绝出站、跨群、缺失或未来引用。存储在私有 `agent-memory.json`，不是第二个消息日志。

引用校验证明来源位置和群身份，不证明语义解释正确。笔记不会自动修改 ack、outbox 或微信群；回复模型也不会自动写回笔记。

## 兼容性

Rust 协议版本与 Python 包版本分别管理。脚本兼容入口依赖完整源码树或已安装包，不再支持只复制几个旧 launcher 就部署。原来的独立运行版保持原样，升级必须停一个、启动一个并核验原状态；不删除 channel/outbox 来绕过命令变化。
