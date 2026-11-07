# Codex Session 插件接入

wx-cli 独立保存微信事件、订阅进度、持久 inbox、处理决定和发送队列。本指南描述项目级 Session 插件：它通过 queue-snapshot 把已有 delivery 接到**同一个原 App Session**，并核对实际回合。此插件不拥有第二份业务账本，也不创建或恢复 App-held Session；终结状态只通过只读 `thread/read` 核对。`integrations/codex.py` / `session_adapter.py` 提供的自有宿主托管连接是另一种既有适配，不在本指南的 Hook 退役范围内。总体边界见[通道订阅合同](CHANNEL_SUBSCRIPTIONS.md)。

本项目级插件使用 queue-snapshot。原 App 聊天宿主必须已加载；自动冷唤醒尚未实现。较早 Hook 测试保留在日期化验证记录中。

## 安装到一个项目

需要已安装的 `wx-agent` wheel 与[插件 bundle](../plugins/codex-wechat/.codex-plugin/plugin.json)。wheel 提供 `wx-agent-queue-session`、`wx-agent-plugin-runner` 和 `wx-agent-plugin-install`；bundle 提供项目 Skill 的唯一文本来源及通用插件包装。`wx-agent-plugin` Hook CLI 已移除。安装只写指定项目的 Skill 和私有安装记录，不写 Hook 配置或全局目录。

先准备下方的基础路由配置 `plugin.json` 和队列配置 `queue-session.json`，限制文件访问，并将私有配置排除版本管理。安装的 `--config` 指向队列配置。项目目录、可执行文件、bundle 都用规范绝对路径。安装示例：

```sh
wx-agent-plugin-install \
  --action install \
  --project /ABS/projects/synthetic-project \
  --config /ABS/private/queue-session.json \
  --executable /ABS/projects/synthetic-project/.local/venv/bin/wx-agent-queue-session \
  --bundle /ABS/distribution/plugins/codex-wechat
```

`--config` 使用队列配置，`--executable` 使用已安装的 `wx-agent-queue-session`。安装器在目标项目写入 `.agents/skills/wechat-inbox/SKILL.md` 和 `.local/` 私有记录；新安装不写 `.codex/hooks.json`。Linked worktree 也只安装到明确的 `--project`，遵循项目本地 Skill 治理。安装、卸载和旧记录退役分别使用 `--action install`、`--action uninstall`、`--action retire-hooks`。后者只处理安装记录中明确归属且未被用户改动的旧 Hook；遗留参数 `--hooks-project` 仅在退役时用于匹配记录的共享来源，不能用于新安装。卸载和退役按记录保护用户改动。

## 基础路由配置

基础配置 `plugin.json` 保存原聊天与通道的映射。下面的路径、ID、主机名和 owner 全是合成占位值。`project` 必须是实际承载原 Session 的项目目录，`agent_host` 必须匹配运行插件的主机名；`session_id` 是 Codex Session ID，`conversation_id` 是微信通道 ID，两者不可互换。

```json
{
  "version": 1,
  "conversation_config": "/ABS/private/conversations.json",
  "wx_agent": "/ABS/venv/bin/wx-agent",
  "codex": "/ABS/bin/codex",
  "gateway_config": "/ABS/private/subscriber-client.json",
  "gateway_client": "/ABS/venv/bin/wx-agent-gateway-client",
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

必填字段为 `version`、`conversation_config`、`wx_agent`、`codex`、`bindings`，每个 binding 必须含示例中的六个字段。本队列接入需要 `gateway_config` 与 `gateway_client`，**必须成对提供**；网关可在同机运行或通过受限 SSH 调用，见[应用网关配置](APPLICATION_CLIENT.md)。从旧 Hook 插件配置迁移时，移除 `remote`、`remote_auth_token_env`、`resume_queued` 字段；它们不属于本 queue-snapshot 插件配置。受限 SSH 网关的异机真实入站读取及范围拒绝已实测；迁移到新的宿主或原聊天时仍须单独复验，不能将连接成功视为原聊天完成处理。配置及凭据留在私有位置，不放入项目 Skill 或公开示例。

## 运行与处理边界

运行本项目级插件的 `wx-agent-plugin-runner` 时默认使用 `queue-snapshot` adapter。现有显式参数 `--adapter queue-snapshot` 仍可使用。原 Session 的工具权限须允许执行项目内的本地 `snapshot`、`propose` 命令。通道访问与发送凭据由外层通知器持有，模型无需为此获得网关 token 或网络访问。通知仅包含本地读取命令、引用和摘要，不包含正文、lease 或凭据。

要在原 Session 宿主上持续检查待办，可以前台进程启动有界通知循环：

```sh
wx-agent-plugin-runner --config /ABS/private/queue-session.json --seconds 3600 --interval 2
```

`--seconds` 是整数，范围为 1–86400，默认 3600；`--interval` 至少为 1 秒，默认 2 秒。runner 使用由私有配置路径确定的私有运行目录，持有独占锁并记录不含消息正文、token 或 Session ID 的 `health.json`；同一配置的第二个 runner 会拒绝启动，也不会改写当前运行者的健康记录。`SIGINT` / `SIGTERM` 请求它在当前有界 API 周期结束后停止，写入停止状态并释放锁；它不关闭 Codex 宿主。runner 通过 queue-snapshot 通知原 Session，不自行确认通知、完成业务决定或发送微信消息。

`notify` 先向 Rust inbox 领取并 reserve 原 delivery，再使用宿主原生 `codex queue --thread ... --message ...` 排队。通知只包含 conversation ID、delivery ID 和单向散列的 `notification_ref`；散列用于关联通知，不暴露 lease token。queue 返回成功只表示排队，不是原 Session 已收到或完成处理。未知结果保留原 reservation，不另开回合、不换 ID 重试。

只有精确原 thread/CWD/client ID、规范快照读取、完整冻结范围、明确最终决定和带完成时间的原生成功回合都通过核验，才将决定与 Rust transport 分别记账。Session 必须用原 lease 显式记录 `skip`、`defer`、`draft` 或 `reply`；最终模型文字不会自动发微信。owner、epoch 或原 Session 不符时停止处理，保留原记录。处理命令见[托管会话指南](MANAGED_CONVERSATIONS.md)。独立 `thread/read` 可能将仍运行的回合投影为 `interrupted`；缺少精确终态与有效 `completedAt` 时不得提前结案或另排回合。

当前实机范围：一个真实测试群回复有用户收件回执；另外两个业务群由同一个原 Session 读取四批旧历史，两群各两批，并记录 `skip`，没有 backfill。历史 Hook 结果仍保留在日期化记录中。桌面 App 原 Session 的 queue-snapshot 结果及异机处理范围见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)。

## 队列配置与处理回合

外层 adapter 通过受限网关读取冻结 delivery，将完整事件分页保存为项目内的私有快照，再通过原生 `thread/queue/add` 通知同一个 Session。通知只有本地读取命令、引用和摘要；没有正文、lease 或凭据。默认 `observe` 模式只接受 `skip`、`defer` 或 `draft`。授权回复是显式选项；安装 Skill 不会启用它。

启动 App-held Session 模式前，须让原聊天在其桌面宿主中正常加载。向未加载宿主排队只表示通知已持久化；当前实测直到应用加载原聊天后才收到通知。本插件不自行创建或独立 resume 该聊天；队列及 Rust reservation 保留，宿主恢复后核对原通知。

每个 binding 只使用此 adapter。既有 reservation 会阻止并行通知器接管。原 Session 的权限保持不变，外层只执行其已有网关权限允许的操作。

私有配置使用独立文件：

```json
{
  "version": 1,
  "base_config": "/ABS/private/plugin.json",
  "runtime_dir": "/ABS/projects/synthetic-project/.local/inbox-snapshots",
  "snapshot_cli": "/ABS/projects/synthetic-project/.local/venv/bin/wx-agent-queue-session",
  "proposal_mode": "reader-cli"
}
```

队列配置 `queue-session.json` 的 `base_config` 指向上面的基础路由配置。新接入显式使用 `reader-cli`，由本地 CLI 生成受保护的引用和摘要；既有 `final-json` 记录继续兼容。`runtime_dir` 必须已存在、属于当前用户且为 `0700`；快照和处理证据为 `0600`，不覆盖已有文件。它和 `snapshot_cli` 都须在原 Session 的项目内，供只读模型正常访问。快照是派生输入，Rust 仍拥有 delivery、游标和决定。

```sh
wx-agent-queue-session notify --config /ABS/private/queue-session.json
wx-agent-queue-session reconcile --config /ABS/private/queue-session.json
wx-agent-plugin-runner \
  --config /ABS/private/queue-session.json --seconds 600 --interval 2
```

外层先 claim/reserve，持久化快照与通知意图，再排队一次。模型执行的 `snapshot --file … --ref … --offset …` 子命令仅做本地读取，不加载网关或凭据；每段至多 8192 字节，必须读取完整冻结范围。外层只接受精确 thread/CWD/client ID、成功的规范读取命令、完整分段摘要、明确的最终 JSON 和带有效完成时间的原生成功回合。随后分别记录 turn、提交业务决定、核对 Rust 结果并结束 transport。最终文字本身不算业务确认，也不会发送微信。

合法的 `defer` / `draft` 在终结并经 Rust 显式恢复后，沿用原 delivery ID、原 Session，生成新的 transport 引用；每轮的私有 note、快照和提交标记独立保留。升级前的在途 note 只有与当前 Rust transport 的引用精确匹配时才会采用。旧回合不能作为新一轮的收件或决定证明。

排队或业务提交结果未知时保留原 reservation 与意图，停止重发；`reconcile` 核对原回合，并仅在全部证据成立时记录原决定和 transport。如果业务决定已经成功、仅 transport 结束失败，后续核对只补原 transport，不重复提交决定。该模式的合成检查和实际宿主验收也分别记录，不能用前者宣称后者通过。

## 对询问我们的新消息按需回复

先为确切目标及原 Session 配置网关 `inbox`、`send`、`result` 权限，并将该目标设为当前 owner/epoch 的 `push/authorized`。只发通知的应用仍保持原来的 `send/result`，不借用其他项目凭据。queue snapshot 的私有配置可增加：

```json
{
  "reply_policy": "authorized",
  "reply_not_before": 1700000000,
  "reply_instructions": "只处理明确询问我们且属于当前业务的问题。闲聊跳过；事实不足或需要新的用户决定时延后。",
  "proposal_mode": "reader-cli"
}
```

示例时间为合成值；实际填写本次启用的 UTC 秒。软件只允许引用启用后、冻结范围内的最新入站文字或引用回复；完整历史仍保留供上下文核对，旧消息和本账号出站不能触发回复。Session 还须明确声明 `addressed_to_us=true`，说明理由并引用合格 `source_cursors`。这一步由原业务 Session 判断语义，不因收到一条群消息就必答；引用内容、群消息及附件都不能扩大操作授权。

`reader-cli` 模式让 Session 在读完所有分段后，把语义决定交给本地 `propose --file … --ref …`。CLI 从原快照生成身份和摘要；Session 最终只输出 `WXCLI_PROPOSAL_READY`。外层核对该原回合中成功的规范工具命令、完整 stdout、快照摘要和完成时间。最终标记本身没有发送权，模型不需手抄长摘要。默认 `final-json` 路径及历史在途记录继续兼容。

外层只为有效回复提案提交一次稳定的 `reply-{conversation_id}-{delivery_id}` 业务 key。提交标记先持久化；之后只查同一 key，排队或未知结果不会重发。只有原 attempt 1 已 `ui_observed` 或 `recipient_confirmed`，受限 `complete_observed_reply` 才会登记业务决定；该接口只关联已存在的正向发件记录，不入队、不发送。原生回合终结、业务决定、本地出站和独立收件仍是不同事实。

## 一个原聊天明确接入多个群

默认专属 Session 的单会话隔离不变。只有确需让同一个原聊天处理多个已授权目标时，base config 才设置 `allow_shared_session=true` 并列出这些目标。每个目标还须分别得到当前 owner/epoch 的显式授权：

```sh
wx-cli coordinator --state-dir /ABS/private/ui inbox authorize-shared-session \
  --session-ref codex:synthetic-host:original-thread \
  --conversation-id CONVERSATION --owner CURRENT_OWNER --expected-epoch EPOCH \
  --reason '用户明确选择同一原聊天处理这些目标'
```

各授权按代际留存，旧专属 scope 历史不删除。未授予的第三个目标或失效代际仍被拒绝；同一个实际 Session 引用和原生 thread ID 保持不变。Rust 原子协调同一 Session 的在途处理，通知器先续接在途回合，再处理最早待办，避免两群通知合并或一群持续占用。启用共享绑定期间须使用支持该授权的运行版本；旧可执行文件可能在写入时失败关闭，不能通过删除 scope 或状态回退。

## 分开运行采集、通知与发送

已有共享桌面执行器时，采集器只读取明确列出的会话，不另起执行器或模型宿主：

```sh
wx-agent inbox --config /ABS/private/conversations.json run --collector-only \
  --id CONVERSATION_A --id CONVERSATION_B --seconds 3600 --interval 2
wx-agent-plugin-runner \
  --config /ABS/private/queue-session.json --seconds 3600 --interval 2
```

第一条运行在微信宿主，第二条运行在原 Session 宿主。正常退出会等待当前采集或 API 周期结束，保留原待办、通知及发件状态。持续运行可由对应宿主的普通服务管理器管理；服务启动不解除业务应用的暂停，也不恢复历史批次。App 原聊天未加载时通知仍只排队，不能据进程存活宣称收件；自动冷唤醒继续单独验收。
