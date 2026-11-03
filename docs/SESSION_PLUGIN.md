# Codex Session 插件接入

wx-cli 独立保存微信事件、订阅进度、持久 inbox、处理决定和发送队列。Codex 插件只把已有 delivery 接到**原 Session**，使用宿主原生 hook 核对实际回合。插件不拥有第二份业务账本，也不调用 `thread/start` 或 `thread/resume`；终结状态只通过只读 `thread/read` 核对。总体边界见[通道订阅合同](CHANNEL_SUBSCRIPTIONS.md)。

## 安装到一个项目

需要两件交付物：已安装的 `wx-agent` wheel 提供 `wx-agent-plugin`、`wx-agent-plugin-runner` 和 `wx-agent-plugin-install`；[插件 bundle](../plugins/codex-wechat/.codex-plugin/plugin.json)提供项目 Skill 的唯一文本来源及标准插件包装。脱离源码检出的 wheel 安装时，须单独提供 bundle 的规范绝对路径；wheel 本身不包含该 Skill 副本。

先在仓库外创建私有 `plugin.json`，填入下方配置中的真实授权值，并限制文件访问。项目目录、可执行文件、bundle 都用存在的规范绝对路径。安装示例：

```sh
wx-agent-plugin-install \
  --project /ABS/projects/synthetic-project \
  --config /ABS/private/plugin.json \
  --executable /ABS/venv/bin/wx-agent-plugin \
  --bundle /ABS/distribution/plugins/codex-wechat
```

普通项目默认合并该项目的 `.codex/hooks.json`。Linked Git worktree 的原生 Hook 来源位于主 checkout 对应目录；安装器默认在写入前拒绝，并提示所需路径。明确授权共享来源后，在上述命令追加 `--hooks-project /ABS/projects/main-checkout`；子目录项目须指定主 checkout 中相同相对路径的目录。安装器核对同一 Git common repository 和对应路径，拒绝任意其他目录及符号链接。共享 Hook 可能影响关联工作树，私有配置仍须精确限制原 Session 和工作树 CWD。

`--project` 始终承载 `.agents/skills/wechat-inbox/SKILL.md` 和 `.local/` 安装记录；`--hooks-project` 只承载 `.codex/hooks.json`。结果及记录明确列出这两个范围。已有其他 hooks 保留；同参重装幂等，异义内容或用户改动不会被覆盖。Hook 来源中的 hooks 文件和请求项目中的安装记录须分别由 Git 忽略规则排除版本管理。安装不会写 `~/.codex`、`~/.agents`、`~/.claude` 或全局插件缓存，也不会自动信任 hook；Codex 宿主仍须按正常流程审查和启用项目 hook。卸载使用同一入口的 `--action uninstall --project /ABS/projects/synthetic-project`，从记录恢复 Hook 位置，只移除自有三项及安装器创建且未改动的项目 Skill；不含 `hooks_project` 的旧 v1 记录仍按原项目本地位置卸载。

## 私有配置

下面的路径、ID、主机名和 owner 全是合成占位值。`project` 必须是实际承载原 Session 的项目目录，`agent_host` 必须匹配运行插件的主机名；`session_id` 是 Codex Session ID，`conversation_id` 是微信通道 ID，两者不可互换。

```json
{
  "version": 1,
  "conversation_config": "/ABS/private/conversations.json",
  "wx_agent": "/ABS/venv/bin/wx-agent",
  "codex": "/ABS/bin/codex",
  "bindings": [
    {
      "session_id": "synthetic-session",
      "conversation_id": "0123456789abcdef0123456789abcdef",
      "owner": "synthetic_owner",
      "expected_epoch": 1,
      "project": "/ABS/projects/synthetic-project",
      "agent_host": "synthetic-host"
    }
  ]
}
```

必填字段为 `version`、`conversation_config`、`wx_agent`、`codex`、`bindings`，每个 binding 必须含示例中的六个字段。可选 `remote`、`remote_auth_token_env`、`resume_queued` 用于原生 `codex queue` 连接；`resume_queued` 默认 `false`，它是 queue 参数，不是插件直接调用 `thread/resume`。跨宿主 inbox 连接可选 `gateway_config` 与 `gateway_client`，**必须成对提供**。受限 SSH 网关的异机真实入站读取及范围拒绝已实测；由另一宿主的原 Session 完成插件处理仍需单独验收。配置及凭据留在私有位置，不放入项目 Skill 或公开示例。

## 运行与处理边界

入口是 `wx-agent-plugin --config /ABS/private/plugin.json hook|notify|status`。项目 hook 把原生 JSON 从 stdin 交给 `hook`，并把原生 JSON 响应送回宿主。注册的事件是 `SessionStart`、`UserPromptSubmit`、`PostToolUse`，hook 超时 60 秒，附加上下文上限 1200。`SessionStart` 仅提示待办，不领取；`UserPromptSubmit` / `PostToolUse` 在实际回合中提供受限读取提示。插件不采用 `PreModelRequest` 注入正文、凭据或 lease token。

原 Session 的工具权限须允许调用配置中的通道或受限网关。已有 Session 可能保留先前的权限范围；服务器启动选项不一定覆盖它。应通过宿主正常的会话权限设置核对实际访问，再启用通知。

需要主动检查时，可运行：

```sh
wx-agent-plugin --config /ABS/private/plugin.json status
wx-agent-plugin --config /ABS/private/plugin.json notify
```

要在原 Session 宿主上持续检查待办，可以前台进程启动有界通知循环：

```sh
wx-agent-plugin-runner --config /ABS/private/plugin.json --seconds 3600 --interval 2
```

`--seconds` 是整数，范围为 1–86400，默认 3600；`--interval` 至少为 1 秒，默认 2 秒。runner 使用由私有配置路径确定的私有运行目录，持有独占锁并记录不含消息正文、token 或 Session ID 的 `health.json`；同一配置的第二个 runner 会拒绝启动，也不会改写当前运行者的健康记录。`SIGINT` / `SIGTERM` 请求它在当前有界 API 周期结束后停止，写入停止状态并释放锁；它不关闭 Codex 宿主。runner 周期性调用同一插件的 `notify`，不自行确认通知、完成业务决定或发送微信消息。

`notify` 先向 Rust inbox 领取并 reserve 原 delivery，再使用宿主原生 `codex queue --thread ... --message ...` 排队。通知只包含 conversation ID、delivery ID 和单向散列的 `notification_ref`；散列用于关联通知，不暴露 lease token。queue 返回成功只表示排队，不是原 Session 已收到或完成处理。未知结果保留原 reservation，不另开回合、不换 ID 重试。

只有原 Session 的真实 `UserPromptSubmit` 回调携带完全匹配的 `WXCLI_DELIVERY_V1` 通知时，插件才把原生 turn 记录到 Rust。宿主回合终结和业务决定分别保存：Session 必须读取冻结的 delivery/events，并用原 lease 显式记录 `skip`、`defer`、`draft` 或 `reply`；最终模型文字不会自动发微信。owner、epoch 或原 Session 不符时停止处理，保留原记录。处理命令见[托管会话指南](MANAGED_CONVERSATIONS.md)。

独立 `thread/read` 可能把宿主仍在运行的回合投影为 `interrupted`。插件只接受精确原 thread/turn、终态状态及有效 `completedAt` 同时存在的完成证据；缺少完成时间时保留原 transport，不提前结案或排入另一个处理回合。

当前实机验收覆盖专用原生 TUI 宿主持有的原 Session：新入站在同一自动回合内完成精确通知回调、冻结事件读取、一次 `skip` 决定及原生/Rust 双方终结，微信发送保持暂停。普通应用的受限 SSH 发件及独立收件、异机 inbox 读取均有各自证据。桌面 App 持有的原 Session、实际异机 Session 插件处理和图片队列仍分别保留未通过状态。详见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)。
