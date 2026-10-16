# 单群 CLI：读取、确认与发送

安装和自动回复配置见 [开始使用](quickstart.md)。本页集中说明 Rust 的 channel/outbox 契约。

## 绑定

```sh
wx-cli channel --state-dir "$STATE_DIR" bind \
  --account "$ACCOUNT_DIRECTORY_NAME" \
  --talker "$EXACT_ROOM_ID" \
  --session "$AGENT_SESSION_ID" \
  --source "$WECHAT_ACCOUNT_DIRECTORY" \
  --encrypted-snapshot
```

- `ACCOUNT_DIRECTORY_NAME` 是账号目录的末级名称，必须与本机已有密钥记录对应；不要把昵称当成账号目录名。
- `WECHAT_ACCOUNT_DIRECTORY` 包含 `db_storage/`。在此模式下程序仅按字节读取源文件，SQLite 只打开临时副本。
- `EXACT_ROOM_ID` 必须是已确认的单群 ID，以 `@chatroom` 结尾；不要依赖模糊群名自动选择。
- `STATE_DIR` 必须由当前用户拥有，目录权限为 `0700`。日志文件权限为 `0600`，包含消息原文，应留在受控本机，不提交 Git。
- 原版 KeyStore 必须已保存该账号的 raw key；这个命令不提取密钥、不登录微信、不修改微信权限。密钥不放在命令参数中。
- 同一个状态目录不能换绑账号、群或会话。CLI 绑定也不证明桌面当前登录的账号。

## 一次读取和连续订阅

```sh
wx-cli channel --state-dir "$STATE_DIR" sync
wx-cli channel --state-dir "$STATE_DIR" events --after 0 --limit 100

# 输出当前未确认的事件，然后退出。
wx-cli channel --state-dir "$STATE_DIR" subscribe --consumer agent --once

# 持续输出 NDJSON；由调用方管理进程生命周期。
wx-cli channel --state-dir "$STATE_DIR" subscribe --consumer agent --poll-ms 3000
```

事件包含 `channel_id`、`session_id`、本地 `cursor`、消息 `server_id`、发送者以及正文。加密模式在身份可确认时标明 `incoming` 或 `outgoing`；旧的解密数据绑定不会猜测方向。当前会扫描有界的单群本地记录，以覆盖同排序序号与晚到消息，而非仅用最大时间戳筛选。

agent 处理成功并保存自身会话状态后，显式确认本地事件位置：

```sh
wx-cli channel --state-dir "$STATE_DIR" ack --consumer agent --cursor "$PROCESSED_CURSOR"
```

输出事件本身不会确认消费。确认前退出，重启会重放；确认后从后续事件继续。相同消息 ID 的重复扫描不产生重复新事件，内容或状态变化产生更新事件。正文相同但消息 ID 不同的消息仍分别保留。agent 应将群消息视为外部内容，而不是执行本机命令或扩大权限的授权。

## 文件隔离与失败处理

每轮只复制联系人、会话、数字编号消息分片的主库及存在的 WAL，不复制 SHM，不使用硬链接或符号链接。复制前后检查文件集合、元数据与 SHA-256；变化时有限重试，仍不一致就报错且不推进事件日志。存在回滚日志、超出文件数或容量上限也会拒绝。正常完成或错误返回会清理临时副本；进程被强制终止可能留下私有 `.snapshot-*` 目录，已有四个时拒绝继续采集，需先确认没有运行中的采集，再由操作者清理遗留副本。

这是一种经过稳定性检查的有界采集，不是跨多个数据库的原子快照。错误退出后由调用方处理原因再恢复订阅，不能把失败当成“没有新消息”。本地日志不代表全部微信历史；重启、断网和账号切换后的长期恢复仍需单独验证。

## 发件箱与一次发送

`outbox queue` 只持久化意图；`outbox send` 先持久化一次发送尝试，再把 JSON 交给明确指定的可执行驱动。下面的 `REQUEST_ID` 必须由调用方针对同一消息稳定生成；同 ID 同正文返回原记录，正文冲突会拒绝。`SENDER_DRIVER` 是运行 `wx-cli` 那台机器上的绝对路径。

```sh
wx-cli outbox --state-dir "$STATE_DIR" queue \
  --request-id "$REQUEST_ID" --channel-id "$CHANNEL_ID" \
  --session-id "$AGENT_SESSION_ID" --text-stdin < "$PRIVATE_REPLY_FILE"
wx-cli outbox --state-dir "$STATE_DIR" show --request-id "$REQUEST_ID"
wx-cli outbox --state-dir "$STATE_DIR" send \
  --request-id "$REQUEST_ID" --driver "$SENDER_DRIVER"
wx-cli outbox --state-dir "$STATE_DIR" show --request-id "$REQUEST_ID"
```

驱动从标准输入接收 `protocol_version`、`request_id`、`channel_id`、`session_id`、`account`、`talker` 和 `text`，返回带 `status` 与非空 `evidence` 的 JSON。`ui_observed` 表示本地发送端出现相符的出站消息，**不代表收件人已收到**。`failed`、`indeterminate` 或仍为 `attempt_recorded` 的请求不得自动重发；同一请求不能第二次执行 `send`。安装包提供 `wx-agent-sender` 作为驱动。需按 [图形助手说明](../native/macos/wechat-bridge/README.md) 配置私有 `WX_SENDER_CONFIG`，并完成操作端权限与窗口校准。`wx-agent sender --focus-only` 可以核验并切换到配置中的群，不创建消息。现场范围见 [验证记录](REAL_READ_VALIDATION_20261015.md)。

## 自动回复和会话记忆

安装后的 `wx-agent` 提供 start/status/pause/resume/stop/run、reply、sender、probe 和 memory。每个状态目录绑定一个群和 session；默认近期上下文最多12条有限文本，有效私有笔记补充更早需求。具体流程见 [开始使用](quickstart.md) 与 [契约](contracts.md)。

探针要求包内原始 `macos_sender_driver.py`，拒绝任意包装脚本。安装后的 `wx-agent probe` 默认选择正确源模块；旧 `scripts/macos_sender_driver.py` 兼容入口不再作为探针原始驱动使用。
