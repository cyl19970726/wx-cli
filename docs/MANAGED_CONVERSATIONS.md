# 多会话：主动处理与长期 Session 推送

本页描述新增托管入口。工程、实机与独立收件分别见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)；受管 Session 的实际验证范围见[适配器记录](SESSION_ADAPTER_VALIDATION_20261101.md)。旧文字验收不因本次扩展失效，新增功能也不继承旧版的实机验收。

通道订阅与 Session 插件分工见[接入合同](CHANNEL_SUBSCRIPTIONS.md)。下面的 inbox/dispatch 是通道能力，Codex provision/推送是可选适配实验；订阅不要求创建受管 Session。

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

多个会话可以同时提交意图；执行器逐项领取，用该项 channel 的 driver 切到对应目标，核对精确标题、群/私聊类型及草稿后提交，完成本地出站核验再释放桌面。执行期间第二个执行器返回 busy，其他会话仍可读取。队列不依赖默认 selected_id，也不在上一个发送的剪贴板、准备或提交阶段切换会话。

切换会话时，系统输入法提示可能改变空输入框的外观。私有 sender 配置可在 `empty_composer_reference` 之外提供至多两个 `empty_composer_alternates`，格式同为 `{path, sha256}`。每一张都必须是已人工确认无草稿的完整输入区截图，位于私有 UI 目录，尺寸、权限和摘要独立校验。接受其中一张完整像素匹配；不屏蔽提示区域或容忍其他文字/图片。原生准备与提交必须使用同一套参考图，恢复原尝试也必须保留它已使用的参考集合。

同步发送内部入队并有界等待；`--no-wait` 只冻结并入队，交给独立桌面执行器，受管 Session 应采用此方式。返回 `pending` 只表示还未完成，应查询原 ID，不能换 ID 重发。事件→inbox、意图→dispatch 是可对账的跨文件步骤：监听器每轮补 inbox，执行器启动及每 10 秒补 dispatch；也可显式运行 `inbox reconcile`、`dispatch reconcile`，不生成新意图。未带 origin epoch 的旧排队项保留并阻止执行；不把它们自动授权给新 owner。

文字和图片按全局就绪顺序执行，同会话前项失败会阻止后项越过。图文使用相同 `--batch-id` 和从 0 开始的 `--part-index`；即使 part 1 先入队，也等 part 0 成功。前置部分必须是 `done` 且 operation 为 send、result 为 `ui_observed` 或 `recipient_confirmed`；仅有 `done` 不够，`abandoned_pre_submit`、`image_prepare_rejected`、`text_button_guard_rejected` 均不满足依赖。终止归档的 batch 后件保留阻塞，重新授权也不能把归档当成发送成功；显式桌面审查后，新独立批次可以进入正常就绪选择。每部分独立 request ID，不提供原子图文事务。`--priority 0/1/2` 分别表示显式人工、普通业务、主动通知；只影响下次选择，等待超过 300 秒优先按先来顺序，不能越过同会话因果前项。`--expires-at UNIX_SECONDS` 冻结期限，过期返回 owner 检查，不消耗 attempt。图片冻结、输入变化拒绝、像素与出站内容核验沿用[图片合同](IMAGE_SEND_VALIDATION_20261024.md)。队列与 `focus` 共用暂停语义，不在另一个发送中途切窗。

`ui_observed` 仍只是本地出站观察。普通成功后无需人工逐条确认才能发送下一项。不明结果保存原 ID、attempt 和证据并暂停桌面；读取与思考仍可继续。`dispatch resume` 不能把未知项恢复为可发。确认旧 helper 已退出、桌面与草稿安全后，可显式执行：

```sh
wx-agent dispatch --config /ABS/private/conversations.json review \
  --reason '已核对原尝试并确认旧助手退出、桌面草稿安全' --confirm-desktop-safe
```

该命令还必须通过新鲜 helper/桌面 preflight，只恢复其他合格项，不重放未知项。托管模式暂不支持 narrow `recover-unsent`，不能通过旧入口或更换 ID绕过；历史未托管恢复仍按原严格证据合同。

`dispatch abandon-unsent` 只归档**已证明提交前停止**的原调度阻塞，不发送，也不把 outbox 的 `indeterminate` 改成成功、可重试或可恢复。先核对原 dispatch ID、attempt 和私有原生报告，再由已安装版本的 Rust 与注册 driver 在共享 executor/sender 锁下执行完整只读证明：

```sh
wx-agent dispatch --config /ABS/private/conversations.json abandon-unsent \
  --dispatch-id ORIGINAL_DISPATCH_ID --expected-attempt 1 \
  --reason '已核对原尝试在图片准备前停止及完整原生证据'
```

**图片特例**仅接受第一次 attempt 1、当前 outbox `indeterminate`、报告停在 `inspected`，且原 inspect/focus/polls 完整证明同一个已知的六秒搜索弹层未枚举错误。缺少或矛盾的记录、`group_focused`、图片准备/状态/提交痕迹、后续 attempt、未退出的 helper 均拒绝；图片 driver 使用单独的 `--abandon-capabilities` / `--abandon-preflight` 只读合同，不借用图片发送或恢复入口。成功只把原 dispatch 标为 `done/abandoned_pre_submit`，保留原图片意图、attempt、`indeterminate` 和证据；随后仍须按 `review` 合同审查桌面，才能让其他合格队列项继续。**这不授权重试原图片，也不证明图片已发出。**该特例已在 `a9fdce6` 安装版本的原始准备前图片尝试上通过实机证明，原 outbox 和原生证据保持不变。后来停在 `group_focused` 的不同图片尝试被排除在此合同之外；每次仍须核对现场安装版本与原证据，不能把归档当成发送或恢复。

**首次文字 focus 标题拒绝**使用同一归档命令和独立只读 driver 合同 `--focus-abandon-capabilities` / `--focus-abandon-preflight`。只接受已结束的 attempt 1、`inspected/indeterminate`、精确的 client 标题失败和 native `Selected chat header is not exact target and kind`，并复验原 inspect、目标/种类、focus job、选中搜索行的 polls、点击后画面及 helper 已退出。任何 prepare、状态、focus 成功、submit、后续 attempt 或原证据变化均拒绝。成功只归档原 dispatch；原文字 attempt、outbox 与批次后件保持原状，不能继续或重放这个原意图。缺失能力握手的旧 driver 不支持该特例。

若是执行器在 attempt 之前退出，原 outbox 确实仍为零次尝试、原 owner/epoch 未变，可在同一次安全核查加 `--restore-unattempted DISPATCH_ID` 恢复该原请求。任何已有 attempt 的意图都不能走此入口。未关联发件箱证据的本人出站被视为可能人工发言：只接管对应会话并更新 epoch，不暂停其他会话；正在尝试的出站先等待结果再关联，不直接误判为人工。

已完成 transport 的 `defer` / `draft` 在 owner 交接后仍保留原决定和原回合。满足恢复条件时，由**当前** owner 和 epoch 显式执行 `inbox resume`；该操作归档原 turn 历史，并把待办交给当前 owner/epoch，不新建 delivery、不回退源游标。活动或未知 transport 不能走此入口，不能用恢复来重复发送已有请求。

## 逐会话接管

```sh
wx-agent conversations --config /ABS/private/conversations.json takeover --id CONVERSATION --expected-epoch EPOCH
wx-agent conversations --config /ABS/private/conversations.json handback --id CONVERSATION --expected-epoch NEW_EPOCH
```

接管使旧 epoch 失效，只停止该会话新的领取与发送，不暂停其他聊天。已开始的桌面临界区不能承诺撤回；交还恢复原输入方式与回复策略。普通注册表默认选择项改变不会影响带明确 ID 的操作。

## 推送给受管 Session

本节适配器支持由其明确创建、持有的 Codex app-server Session。正在被桌面 App 持有的业务 Session 不会被复制、强行 resume 或换成临时替身。向已有宿主和跨主机接入使用独立的[Session 插件](SESSION_PLUGIN.md)及受限网关；其 Hook 与 queue snapshot 两种模式有各自的宿主能力和验收条件，不把本节受管 Session 的验收替代为桌面 App 验收。

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

代际变化阻断的未尝试发送，只可在同一 owner 重新授权后显式恢复。恢复先在协调器记录精确的原/新代际，再同步 outbox 的代际，最后恢复 dispatch。两个 SQLite 文件不能原子提交；若在两步之间中断，只可按该记录续接同一个零尝试意图。owner、内容、媒体、批次、优先级和到期时间不变，已尝试或未知结果不能借此重放。

冷连接先通过官方 `thread/resume` 加载指定受管 thread，再读取状态；不直接读写存档。配置中的 provisioning 项未 ready 时监听器拒绝启动该配置。初始化是显式 setup；主动读取和普通 metadata 查询不运行初始化模型。

`inbox-health.json` 是私有派生健康信息，记录采集、活跃 Session、需检查事项与队列概况，不替代 Rust 状态。运行期限上限 24 小时；达到期限会关闭自持连接，未完成回合可能中断且保留原投递，需要核查。持续运行验收看实际起止与故障记录，不能把 `--seconds 86400` 当成已经运行 24 小时。

健康信息使用 `run_state=running/stopping/stopped`。退出后只有确认本轮采集器和执行器结束才记录 `stopped_at`；等待超时或清理失败保留 `stopping` 和相应错误。只有持有 runner lock 的实例能写健康文件，最终写入在释放锁之前完成。`push` 带 `push_observed_at`，表示最后一次 Session 观测；关闭连接不代表其业务待办已完成。

## 回退

先停止采集/执行器，核对旧 helper 与 in-flight dispatch，保留所有私有状态。回退代码不回退数据库。旧版对托管 channel 的 attempt 会被拒绝；要读取或处理它们继续使用兼容新版本。没有“删除 marker 即回退”的操作；删除状态、恢复旧 outbox 快照或新建 consumer 都会破坏进度。未迁移会话仍沿用旧入口，迁移后的会话应使用本页流程。


## 图片配置与原请求的后续证据

发送图片前可检查绑定账号的核验凭据：

```sh
wx-cli outbox --state-dir /ABS/private/channel image-verification-status
```

此状态仅说明配置可用，不能代替图片内容核验。V2 图片优先使用该账号已配置的密钥；缺失时可静默从绑定账号及其唯一匹配配置派生，不尝试其他账号、不输出密钥、不写 KeyStore。V1/XOR 兼容原路径。托管 macOS sender 在界面动作之前检查此条件；缺少配置时原请求保持零 attempt，调度为 `blocked/image_verification_unavailable`，其他会话的桌面发送不因此暂停。配置修好后，通过原 `review --restore-unattempted` 流程恢复原请求。

若原图片已取得完整原生准备、一次提交和像素证据，但当时本地内容核验未完成，先仅对原请求读取核验：

```sh
wx-cli outbox --state-dir /ABS/private/channel verify-image-event \
  --request-id ORIGINAL_REQUEST --cursor EXACT_EVENT_CURSOR
```

新候选的明确操作员入口可补记稍后取得的正面证据：

```sh
wx-cli outbox --state-dir /ABS/private/channel reconcile-image-event \
  --request-id ORIGINAL_REQUEST --expected-attempt 1 \
  --cursor EXACT_EVENT_CURSOR --expected-epoch CURRENT_EPOCH \
  --reason '已核对原始一次提交、唯一源事件及完整图片内容证据'
```

该命令不调用 sender、不点击、不重发。它在共享锁和当前代际保护下核对完整原始时间窗、原生准备/提交/截图以及冻结图片与源缓存的解码内容；不能靠选择较早 cursor 隐藏同窗内第二张图片。成功新增不可修改的 observation 与独立报告，推进 intent 的当前 `ui_observed` 状态并结束原调度；原 `indeterminate` attempt 和原报告保持不变，`outbox show` 用 `late_observation` 解释这一差异。桌面暂停仍须显式核查解除。结果仍是本地观察，独立收件另记。

## 已知原生拒绝的独立归档合同

### 首次文字搜索未选中结果的受控恢复

原 attempt 1 若在 `inspected` 阶段结束，原生错误精确为 `Search popup had no unique category-eligible exact target within six seconds` 或 `Search popup not enumerated by ScreenCaptureKit within six seconds`，可由维护方在修好目标定位后请求一次同 key 的 attempt 2。前者要求完整 popup 截图及从未选中的 poll，后者要求所有 poll 都是唯一主窗口、无 popup、无选中，并使用它自己的完整终态文件集合；两种证明不混用。它不适用于已点击搜索结果的标题拒绝、一般超时、其他 focus 错误、图片或已进入 prepare/submit 的请求。

```sh
wx-cli coordinator --state-dir /ABS/private/ui dispatch recover-unsent \
  --dispatch-id ORIGINAL_DISPATCH_ID --request-id ORIGINAL_REQUEST \
  --expected-attempt 1 --owner ORIGINAL_OWNER --expected-epoch ORIGINAL_EPOCH \
  --reason '已核对完整首次搜索终态、无选中/prepare/submit及修复后的桌面定位'
```

协调器要求原 dispatch 为 `unknown/indeterminate`、surface 已暂停且空闲、原 owner/epoch 仍 active/authorized。只能调用该 channel 注册的 driver；独立 `--focus-recovery-capabilities` / `--focus-recovery-preflight` 验证原 text、目标类型、PID/窗口、inspect/focus 终态、完整搜索截图、从未选中的有界 poll、无 prepare/submit、无后续 attempt 或存活 helper，并在慢检查后重核文件摘要。当前助手和桌面须另行通过实时预检，不能把旧截图权限当作新签名的授权证明。

Rust 持有共享 executor/sender 锁，在第二次 CAS 后先保存受控恢复 reservation 和审查记录，再以原 immutable intent 新增 attempt 2。原 request/publisher/正文、dispatch、owner/epoch、attempt 1 和原失败文件均保留。新 attempt 仍先 inspect/focus/验证草稿，再允许一次 submit；原生回合或进程未知时不自动重试第三次。恢复结束仍保留 surface 暂停；仅在原结果明确并经正常桌面审查后才能 `review` 后续业务。进程中断保留 reservation，不会重排这个原请求。

视觉标题仅规范 Unicode 空白，其余字符、标点、分类和群成员数要求不放宽。操作前元数据查询覆盖空白等价的所有候选，跨群/私聊重名一并拒绝；存储身份、目标及事务字段保持原字节，不用 OCR 文本替换真实绑定。

连续向同一聊天发件时，支持新证据合同的 sender 用 `allowCurrentHeader=true` 显式选择：原生助手在同 PID/窗口/frame/前台且没有额外弹窗的条件下，两次间隔至少 120 毫秒采集并验证精确标题和类型。省略或为 false 的旧调用方仍走原搜索合同。群标题仍要求成员数，私聊保持独立标题规则。成功保存明确的 `current-header`、`group-header` / `private-header` 与 `headerConfirmations=2` 证据，不伪装为搜索结果分类；两次不匹配则走原完整搜索。prepare/submit 仍核对同一新鲜 focus 引用、目标、类型和草稿，不能凭上一次已发送就省去当前核验。

以下入口用于操作员在完整只读证据下退役**原调度阻塞**。它们不重试、不清空或替换草稿，不把原 outbox/attempt/client report 改为成功、未发送或可重发，也不自动解除暂停。先核对安装版本、原 dispatch ID、原 attempt 1、当前绑定/owner/control 和私有原生文件；源码、合成检查或一次失败归档都不能证明现场已启用修复。

两条命令均由注册 driver 提供独立 capability/preflight，Rust 持有原 `executor.lock` 与 `sender.lock`，核对当前/已结束的原 attempt、规范私有文件、冻结内容和 SHA，并在慢证明后重复核查，以数据库 CAS 结束同一 dispatch。缺失、链接文件、越界/过大文件、身份/正文/媒体/校准冲突、后续 attempt、迟到观察、提交后痕迹或活跃 helper 均拒绝。证明期间只读，不启动 GUI、复制、发送或原生助手。

### 图片 Copy 计数拒绝

```sh
wx-agent dispatch --config /ABS/private/conversations.json archive-image-copy-rejection \
  --dispatch-id ORIGINAL_IMAGE_DISPATCH_ID --expected-attempt 1 \
  --reason '已核对首次图片 prepare 的已知 Copy 拒绝及完整无 submit 证据'
```

只接受 report `group_focused/indeterminate`、Native prepare `ok=false`，以及精确旧错误 `Composer Copy did not replace image sentinel exactly once (types: TYPE_LIST)`。生产形式在冒号后有**一个 ASCII 空格**；兼容旧零空格形式，不接受任意空白。实际类型列表必须包含 `public.png` 或 `public.tiff`，不能用空列表、仅文件 URL 或其他错误替代。prepare job 的冻结图片、inspect/focus、目标类型、PID/窗口/frame、当前配置与 visual inputs 必须一致；state 只能是 `prepare-attempted`，不能已有 prepared 像素、Copy 类型、submit job/result/marker 或提交状态。

driver 合同为 `--image-rejection-capabilities` / `--image-rejection-preflight`。成功只记原 dispatch `done/image_prepare_rejected`，原 outbox/attempt 继续 `indeterminate`，原图草稿和原生证据保留。原 `abandon-unsent` 的 `image_before_prepare_only` 边界不变，仍拒绝这类已进入图片准备的错误；不能借图片发送或恢复入口处理。

### 文字首次 Send 按钮防护拒绝

```sh
wx-agent dispatch --config /ABS/private/conversations.json archive-text-button-rejection \
  --dispatch-id ORIGINAL_TEXT_DISPATCH_ID --expected-attempt 1 \
  --reason '已核对原文字准备成功、首次 Send 按钮防护拒绝且无提交 marker'
```

此入口只处理第一次文字 attempt 的以下完整组合：

- 原 report 为 `prepared/indeterminate`，别名精确为 `submit outcome uncertain: Send button not confirmed`，且没有 `observation`、`event_cursor` 或确认提交后的计时字段。
- Native prepare 为 `ok=true/prepared=true`，动作、目标和三项权限证据完整；原 inspect/focus/job/evidence 与当前配置、PID/窗口/frame 一致。
- prepare/submit job 的冻结文字逐字相同，native visual state 仍为同一事务的 `prepared`；正文、旧草稿预期、目标类型及校准参数均匹配。
- Native submit 的失败结果仅有 `id`、`ok=false`、`error`、`permissions`，错误精确为 `Expected exactly one visible Send label in lower conversation pane`，三项权限为 true；**`submit-attempted` marker 必须不存在**。
- 无 `submit-attempted` / `submitted-unverified` state、`submit-immediate` / 点击后痕迹、额外或后续 attempt、迟到观察或活跃 helper。其他错误、准备失败、缺证据或文字改变均不适用。

独立 driver 合同为 `--text-button-rejection-capabilities` / `--text-button-rejection-preflight`；capability 包含 `protocol_version=1`、`text_button_rejection_protocol_version=1`、`prepared_text_only=true`、`submit_marker_forbidden=true`，成功证明只返回相同两个版本字段及 `button_rejection_proven=true`。原文字 `abandon-unsent` / recovery 的合同不变。

成功仅记原 dispatch `done/text_button_guard_rejected`；原 outbox/attempt/client report 仍为 `indeterminate`，已准备的原文字草稿和所有原生文件保持不变，桌面暂停也不变。该结果不声明未发送或送达，不满足同 batch 的发送成功前置条件。原 N5 caption 已有 attempt 1，不能走 `review --restore-unattempted`，不能换 ID 重放。

### 归档后的桌面审查与新业务

归档后仍按本页 `review --confirm-desktop-safe` 合同核查原 helper 已退出、当前窗口/目标/草稿安全；归档本身没有处理遗留草稿。新独立业务请求只有在其自身授权、内容与草稿守卫成立后才可执行，不能把它伪装成旧 N4 或 N5 caption 的恢复。显式替换已知旧图片草稿时，可在私有 sender 配置提供 `expected_draft_media` 的完整冻结图片描述符，与非空 `expected_draft` 互斥。Python 在配置、请求及 prepare 前复验私有文件、类型、大小和摘要，再传递给原生 `expectedDraftMedia`；原生必须先从已核对的 composer 复制并逐像素确认旧图，才允许准备新内容。不得自动推断旧图、清空未知草稿或复用原请求。

原生按钮修复仍必须在**窄按钮区**取得唯一精确 `Send` 并验证校准位置；宽下方面板只排除额外的精确 `Send`，不把草稿中的“发送”字样或非精确标签文本当成第二个按钮。真实额外精确标签、目标/窗口/位置不符仍拒绝。修复不授予弱点击、其他发送入口或重试权限；部署和新图后配文实机验收另记。各原请求、修复部署与 CI 的最新阶段见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md#本轮-n5-延迟对账n4-归档拒绝与-n5-配文停止点2026-11-03)。
