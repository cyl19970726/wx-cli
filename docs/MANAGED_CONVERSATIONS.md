# 多会话：主动处理与长期 Session 推送

本页描述新增托管入口。工程、实机与独立收件分别见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)；受管 Session 的实际验证范围见[适配器记录](SESSION_ADAPTER_VALIDATION_20261101.md)。旧文字验收不因本次扩展失效，新增功能也不继承旧版的实机验收。

群聊和私聊使用同样的稳定会话 ID。每个聊天绑定自己的 owner、输入方式和控制代际；可以主动读取，也可以把增量通知交给该 owner 的持久 Session。收到推送不等于必须回复，Session 的最终文本和工具日志不会自动转发微信。

## 从已有授权会话迁移

使用现有私有 conversations 配置、原 channel 和原 outbox。先干净停止旧 runner，保存私有配置、原绑定、consumer、pending、lease、全部发件历史及冻结图片。已有 pending 或交互租约必须先按原合同处理；不删除它们。下面所有 ID 均为示意，替换成授权目标的真实 CLI 结果；所有自动操作显式携带 `--id`。

```sh
wx-agent conversations --config /ABS/private/conversations.json status --id CONVERSATION
wx-agent conversations --config /ABS/private/conversations.json configure \
  --id CONVERSATION --owner project-session-a --input pull --reply-policy observe
```

新绑定默认 `observe`。已明确授权持续回复的会话迁移时选择 `--reply-policy authorized`，保留原授权；不要求重新逐条审批。`draft` 允许保留待审文案，不能发送。`configure` 返回 epoch；改 owner、输入方式或策略时带 `--expected-epoch` 比较更新。不要把“迁移”当成给未经授权的新聊天开放回复。

首次注册记录事件 cutover；历史消息不重新推送，旧 consumer 不回退。注册之后的入站无论 pull/push、暂停与否，都先进入 Rust inbox；输入方式只决定是否主动通知 Session。源事件更新与非文本保留引用；附件未同步仍为等待状态，不能直接记为已理解。只读分页不领取处理权、不确认 consumer，也不获取桌面锁。

独立候选使用不同 Rust 可执行路径时，已托管且停止的旧 controller 可以在当前操作中使用候选 CLI；原 controller 文件、命令、state 和 consumer 保持不变。活跃旧 writer 或未托管的 API 路径漂移仍拒绝。读取目录元数据不协调其他 controller。一个持久 Session 引用只能绑定一个聊天，不能把不同聊天的上下文合到同一个 thread。

已托管 channel 不再运行旧的单群文案 runner，避免它跳过非文本或绕过处理记录。旧 `chat` 的发送路径与通用 CLI 一样走托管队列；旧版 outbox 领取受到数据库触发器保护。运行新版本时不要另起旧 writer。

## 主动处理一次增量

```sh
wx-agent inbox --config /ABS/private/conversations.json reconcile --id CONVERSATION
wx-agent inbox --config /ABS/private/conversations.json claim --id CONVERSATION --owner project-session-a
wx-agent conversations --config /ABS/private/conversations.json read --id CONVERSATION --after CURSOR --limit 50
```

领取返回 delivery、token、owner epoch 与 cursor 范围。先读取范围内全部消息，再决定 `skip`、`defer`、`draft` 或 `reply`。`show --delivery-id DELIVERY --owner OWNER` 可查看原领取及投递证据；不能以重复通知另建任务。

```sh
wx-agent inbox --config /ABS/private/conversations.json complete \
  --delivery-id DELIVERY --token TOKEN --decision skip --reason '无需回复'
wx-agent inbox --config /ABS/private/conversations.json complete \
  --delivery-id DELIVERY --token TOKEN --decision defer \
  --reason '需要外部确认' --condition '等待排期确认' --due-at UNIX_SECONDS
wx-agent inbox --config /ABS/private/conversations.json complete \
  --delivery-id DELIVERY --token TOKEN --decision draft --draft-stdin \
  --reason '文案待审' --condition '等待负责人审阅' < /ABS/private/draft.txt
```

defer/draft 保存为持续待办。处理游标前进不删除它们；`inbox status --id` 可继续发现。defer 的期限到达或显式设置的 `--condition new_information` 遇到新入站时重新进入待办；其他条件满足后用 `inbox resume --delivery-id … --owner … --expected-epoch …` 明确续办。待审草稿到期不会自动发送；`inbox cancel --delivery-id … --owner … --expected-epoch … --reason …` 显式取消。待办重开或取消保留决定、原草稿与来源历史。

`inbox events --delivery-id … --owner …` 对已冻结范围分页，只读本地日志，不再次采集微信源，也不确认处理。受管 Session 可只拥有自己的 channel/协调器写入范围，直接用此入口读取通知中的源事件。

## 文字和图片共享队列

托管发送必须携带产生该决定的 owner 和 epoch；不能临发送时自动换成最新 owner。request ID 只代表一个不可变意图，重复执行不会产生第二次 attempt。

```sh
wx-agent conversations --config /ABS/private/conversations.json send \
  --id CONVERSATION --owner project-session-a --expected-epoch EPOCH \
  --request-id stable-text-001 --text-stdin < /ABS/private/message.txt
wx-agent conversations --config /ABS/private/conversations.json send-image \
  --id CONVERSATION --owner project-session-a --expected-epoch EPOCH \
  --request-id stable-image-001 --image '/ABS/private/中文 截图.png'
wx-agent dispatch --config /ABS/private/conversations.json status
wx-agent dispatch --config /ABS/private/conversations.json run --once
```

同一 macOS 桌面的配置共用原 `ui_dir`，即唯一协调器根目录。`executor.lock` 排除多个执行循环；Rust 在原 `sender.lock` 内核验领取、旧 helper、窗口和权限，然后才持久记录 attempt。锁传递给 driver，避免重复自锁；父进程退出后 driver 仍持有相同锁。原 helper 未解决标记继续阻止下一次 GUI 操作。正文、图片、目标与 attempt 均在原 Rust outbox；协调器只保存引用与调度信息。

同步发送内部入队并有界等待；`--no-wait` 只冻结并入队，交给独立桌面执行器，受管 Session 应采用此方式。返回 `pending` 只表示还未完成，应查询原 ID，不能换 ID 重发。事件→inbox、意图→dispatch 是可对账的跨文件步骤：监听器每轮补 inbox，执行器启动及每 10 秒补 dispatch；也可显式运行 `inbox reconcile`、`dispatch reconcile`，不生成新意图。未带 origin epoch 的旧排队项保留并阻止执行；不把它们自动授权给新 owner。

文字和图片按全局就绪顺序执行，同会话前项失败会阻止后项越过。图文使用相同 `--batch-id` 和从 0 开始的 `--part-index`；即使 part 1 先入队，也等 part 0 成功。每部分独立 request ID，不提供原子图文事务。`--priority 0/1/2` 分别表示显式人工、普通业务、主动通知；只影响下次选择，等待超过 300 秒优先按先来顺序，不能越过同会话因果前项。`--expires-at UNIX_SECONDS` 冻结期限，过期返回 owner 检查，不消耗 attempt。图片冻结、输入变化拒绝、像素与出站内容核验沿用[图片合同](IMAGE_SEND_VALIDATION_20261024.md)。队列与 `focus` 共用暂停语义，不在另一个发送中途切窗。

`ui_observed` 仍只是本地出站观察。普通成功后无需人工逐条确认才能发送下一项。不明结果保存原 ID、attempt 和证据并暂停桌面；读取与思考仍可继续。`dispatch resume` 不能把未知项恢复为可发。确认旧 helper 已退出、桌面与草稿安全后，可显式执行：

```sh
wx-agent dispatch --config /ABS/private/conversations.json review \
  --reason '已核对原尝试并确认旧助手退出、桌面草稿安全' --confirm-desktop-safe
```

该命令还必须通过新鲜 helper/桌面 preflight，只恢复其他合格项，不重放未知项。托管模式暂不支持 narrow `recover-unsent`，不能通过旧入口或更换 ID绕过；历史未托管恢复仍按原严格证据合同。

若是执行器在 attempt 之前退出，原 outbox 确实仍为零次尝试、原 owner/epoch 未变，可在同一次安全核查加 `--restore-unattempted DISPATCH_ID` 恢复该原请求。任何已有 attempt 的意图都不能走此入口。未关联发件箱证据的本人出站被视为可能人工发言：只接管对应会话并更新 epoch，不暂停其他会话；正在尝试的出站先等待结果再关联，不直接误判为人工。

## 逐会话接管

```sh
wx-agent conversations --config /ABS/private/conversations.json takeover --id CONVERSATION --expected-epoch EPOCH
wx-agent conversations --config /ABS/private/conversations.json handback --id CONVERSATION --expected-epoch NEW_EPOCH
```

接管使旧 epoch 失效，只停止该会话新的领取与发送，不暂停其他聊天。已开始的桌面临界区不能承诺撤回；交还恢复原输入方式与回复策略。普通注册表默认选择项改变不会影响带明确 ID 的操作。

## 推送给受管 Session

当前适配器仅支持由本适配器明确创建、持有的 Codex app-server Session。正在被桌面 App 持有的业务 Session 不会被复制、强行 resume 或换成临时替身；这些 Session 继续主动使用上面的 CLI。跨主机推送尚未接入；Session 和微信协调器需在同一执行宿主，控制电脑只需通过已认证 SSH 操作入口。

已停止的导入 controller 可能保留原稳定 CLI 生成的 selection 指纹。受管候选只可接受 `wx_cli` 不同、其余配置逐字段相同的临时替代指纹，并在本次内存操作中保留原指纹；不得改写共享 selection 来迫使校验通过。

明确新建一个受管 Session 时才执行 provision。该命令运行一次只读初始化回合（不含微信数据、不调用工具或发送消息），随后关闭连接，用同一 thread ID 冷续接验证历史存在，再标记 ready。原引用在初始化 RPC 前保存；中断、未知结果或冷续接失败保留原引用并阻止重复创建。新会话不能只凭 `thread/start` 返回 ID 就宣称已可恢复。同 owner 重复 provision 拒绝替换：

```sh
wx-agent inbox --config /ABS/private/conversations.json provision \
  --session-config /ABS/private/sessions.json --owner project-session-a \
  --cwd /ABS/project --codex /ABS/bin/codex \
  --writable-root /ABS/private/ui --writable-root /ABS/private/channel-a
wx-agent conversations --config /ABS/private/conversations.json configure \
  --id CONVERSATION --owner project-session-a --expected-epoch EPOCH \
  --input push --reply-policy authorized
wx-agent inbox --config /ABS/private/conversations.json run \
  --session-config /ABS/private/sessions.json --codex /ABS/bin/codex --seconds 86400
```

写入范围必须来自项目授权，包含协调器和该 owner 的 channel；不授予原始微信数据库写权限。不配置时保留原只读策略，不能假定 model 能完成业务回执。显式 roots 使用 Codex 的 workspaceWrite 和禁止网络/权限升级策略；其配置形状见 [官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)。已首次登记的 Session 引用保存在 Rust；仅改 sessions.json 不能静默换 thread，替换需显式 configure 更新 epoch。

监听每次先持久采集再通知，源采集并发上限为 2，新事件按 1 秒静默、最长 5 秒合并，每个冻结范围最多 256 项。忙时保留 inbox；Rust 在 `turn/start` 之前保留投递，同 Session 不能并发 reserve。连接重启仍使用原 thread ID，查询已记录 turn 后更新 transport 结果；没有 turn ID 的未知 reservation 留待核查。transport 完成与业务决定分开记录；模型必须通过 inbox CLI 明确完成业务处理，日志与最终回答不会进入微信。采集使用独立线程，监听进程持有 Session 连接，桌面执行器为独立进程；长 GUI 操作不阻塞采集，正常停止让当前发送先走到安全边界。

冷连接先通过官方 `thread/resume` 加载指定受管 thread，再读取状态；不直接读写存档。配置中的 provisioning 项未 ready 时监听器拒绝启动该配置。初始化是显式 setup；主动读取和普通 metadata 查询不运行初始化模型。

`inbox-health.json` 是私有派生健康信息，记录采集、活跃 Session、需检查事项与队列概况，不替代 Rust 状态。运行期限上限 24 小时；达到期限会关闭自持连接，未完成回合可能中断且保留原投递，需要核查。持续运行验收看实际起止与故障记录，不能把 `--seconds 86400` 当成已经运行 24 小时。

## 回退

先停止采集/执行器，核对旧 helper 与 in-flight dispatch，保留所有私有状态。回退代码不回退数据库。旧版对托管 channel 的 attempt 会被拒绝；要读取或处理它们继续使用兼容新版本。没有“删除 marker 即回退”的操作；删除状态、恢复旧 outbox 快照或新建 consumer 都会破坏进度。未迁移会话仍沿用旧入口，迁移后的会话应使用本页流程。
