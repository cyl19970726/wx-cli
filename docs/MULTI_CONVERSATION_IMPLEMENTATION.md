# 多会话实施与验收记录（更新至 2026-11-03）

实施候选已交付为 [草稿 PR #6](https://github.com/cyl19970726/wx-cli/pull/6)。Mac mini 当前已启用 `a9fdce6`，包含此前完成时间防护、核心通道与前台 runner；原生助手未重建。Candidate 3 固定二进制 SHA-256 仍为 `5e2e47ef08789e2eb91dc9388ea11f8e294325127955d877f00a550807946787`。本页区分代码、离线检查、真实读取、Session、GUI 和独立收件证据；旧失败作为历史保留。新版已有原专用 native TUI Session 的全新入站单回合处理及正确传输终态证据；它不覆盖桌面 App 持有的 Session 或实际异机插件。图片聚焦失败的限定归档已在原请求上通过，随后原排队 focus 实际完成；原图片 outbox 的不明 attempt 保留。新的不同图片进入准备阶段后被复制核验拒绝，尚未提交，详见末尾更新。两个授权目标已恢复 `push/observe`，全局桌面发送暂停；不合并、不发布、不向非测试业务群发送。

最新架构决策：wx-cli 独立提供群订阅和消息投递能力，普通应用可发布通知，Session 经 plugin 消费订阅。当前源码已分离旧 Codex 适配、增加应用 `publisher_id`、项目级原生 Hook 插件、鉴权 RPC/SSH 客户端及有界前台通知 runner。通道与宿主分别验收：原专用 native TUI Session 路径已有新版全新入站闭环；不同物理宿主的只读 inbox API 已通过限定范围的实机检查；桌面 App 持有的原 Session 和实际异机 Session 插件投递仍未通过。

## 分批状态

| 批次 | 已有证据 | 未关闭的验收 |
| --- | --- | --- |
| P0 图片/文字基线 | 复用图片、生命周期和恢复修复。私聊与测试群文字、测试群四色合成图片曾获独立收件确认；图片源事件 cursor 49。Candidate 3 当前固定安装版本见上方摘要。 | 旧私聊图片尝试仍保留不明状态。较早群图片的准备前调度已严格归档，outbox 不变；新的不同群图片已聚焦并粘贴，但复制核验拒绝，无 submit。归档不提供图片恢复或重试。 |
| P1 会话隔离 | 逐会话锁、显式 ID、目标 controller 协调和 owner/epoch 已实现；两个授权 channel 并发增量读取成功。最新跨会话入队时，私聊读取与群发送并行。有界接管隔离检查 `complete=true`：同目标双并发 takeover 只有一个 CAS 成功，另一会话读取成功且绑定未变；旧/接管中/迟到发送均被代际拒绝，未产生意图、attempt 或桌面效果；正确 handback 恢复原 owner、`push/observe`、Session 引用和处理水位。 | 人工出站检测的现场复验及更广故障场景仍未闭合；单次有界接管检查不等于长期并发验收。 |
| P2 共享发送 | 文字/图片/focus 共用 Rust dispatch，含来源代际、batch 顺序、优先级、到期及未知结果保护。最新同群 Q2/Q3 各一次 attempt、`ui_observed`，源 cursor 51/52，第二执行器返回 busy；跨会话队列按 dispatch 22 私聊、23 群串行完成，各一次 attempt，源 cursor 12/54。用户独立确认两组文字的目标、顺序、内容、换行和 emoji 正确；重复 ID 未增加 attempt。 | 图片队列尚未成功；较早图片调度严格归档后，原排队 focus 已完成。新的不同图片在准备阶段停止，不明调度与零 attempt 配文均保留。这些有界场景不替代更广故障注入或长期运行验收。 |
| P3 inbox/Session | Rust 持久批次、处理租约和明确决定已有；底层订阅可重读。`d2a0dae` 的三个项目 Hook 已通过正常原生信任。全新入站在**原专用 native TUI Session** 一个自动回合完成精确回调、冻结 `show/events` 读取和 `skip`；Rust transport 与带完成时间的原生 turn 均为 `completed`，无需操作员恢复，正式私有验收报告 12 项均通过。另一个物理宿主凭限定只读凭据通过真实 SSH 读取该入站，越界读取与发送被拒绝。 | 旧 delivery 的 Rust `interrupted` 历史异常保持原样；只读异机 inbox API 不等于异机 Session 插件投递。桌面 App 持有的业务 Session、实际异机插件路径未通过，私聊旧 pending 保留。 |
| P4 运行验收 | `36ec086`、`09ea8be`、`d2a0dae` 均四项 CI 通过；前者 Rust 861 passed/11 ignored、脚本 130 passed、最终安装包 184 passed；后两阶段本地及 Mini Python 3.9 安装包分别 193/195 passed、脚本各 130 passed。`a9fdce6` 亦已四项 CI 通过并启用；本地与 Mini 脚本 136 passed、已安装包 195 passed，未重建原生助手。两普通应用受限 SSH 发送及独立收件确认有实机证据；中性采集与新 runner 的 300 秒有界运行均正常退出码 0，所属子进程已退出。 | 实际异机插件投递、桌面 App 持有的 Session、真实图片队列及更广现场故障场景未闭合。完整 24 小时观察已取消，不宣称通过。 |

## 工程验证

文字验收候选 `c8f0f0c` 的 [CI 41482737801](https://github.com/cyl19970726/wx-cli/actions/runs/41482737801) 四项成功。`c61ab03` 的 [CI 41572232120](https://github.com/cyl19970726/wx-cli/actions/runs/41572232120)、`36ec086` 的 [CI 41583448361](https://github.com/cyl19970726/wx-cli/actions/runs/41583448361)、`09ea8be` 的 [CI 41588767586](https://github.com/cyl19970726/wx-cli/actions/runs/41588767586) 、`d2a0dae` 的 [CI 41590868934](https://github.com/cyl19970726/wx-cli/actions/runs/41590868934) 和 `a9fdce6` 的 [CI 41609413281](https://github.com/cyl19970726/wx-cli/actions/runs/41609413281) 均四项成功。下表是 `c61ab03` 历史检查；新版本的精确计数见后面的 2026-11-03 更新。本地 Python 3.9 早期 `ensurepip` SIGABRT 不计作产品测试。

| 检查 | 实际结果 |
| --- | --- |
| Rust 完整测试 | 859 passed、11 原有 ignored、0 failed；严格 all-target Clippy、fmt 和 build 通过。 |
| 编译 CLI 合成集成 | 先前 19 项通过，覆盖队列恢复、锁忙、顺序及未知结果；实际文字队列结果另见上方 P2。合成 CLI 不是真实微信。 |
| Python 仓库脚本 | 当前 Python 3.12：119 passed、0 skipped，包含编译 CLI 集成及 sender 证明回归。 |
| 仓库外 wheel | 当前 Python 3.12：123 passed；已安装包与源码检查分开记录。 |
| Mac mini Python 3.9.6 | 当前代码独立构建：仓库脚本 119 passed、wheel 123 passed，无跳过；候选安装和升级前后账本对照通过。 |
| Swift | 参数与命名剪贴板恢复检查通过。输入法 ABC 指示和 caret 的外观差异，采用每配置主引用加最多两张经核实的空输入框校准替代图，仍逐像素匹配整块输入框；没有遮罩或放宽比较。 |
| 可分享材料 | 图源与经原通过回执核验的空白规范化公开 HTML 一并保存；公开源码与 diff 检查通过。图稿不是实现或实机证据。 |

早期并发负载曾使原有两秒进程探针超时；完整复跑与最终 CI 已通过，没有降低断言或新增 Rust 忽略项。早期 Session 写权限、未持久化空 thread 的失败证据均保留。单元测试和安装包检查不代替实机收发。

## 后续兼容修复

GUI 验收期间的源码核查发现：`--no-wait` 在冻结新 outbox 意图、检查复用 ID 的正文/图片/调度参数前就尝试 enqueue。已将非阻塞分支移到相同的身份、内容、状态、metadata 和 driver 检查之后，再入队；仍不执行 GUI 或等待结果。两个单元回归覆盖新文字/图片、复用内容/调度冲突及不明尝试不得入队。新增编译 Rust 集成验证真实冻结、两个零 attempt 排队项、源图片删除后冻结字节仍存在，且没有 driver 调用。

该阶段本地 Python 3.12 仓库脚本 **114 passed**（含编译 CLI 的 **17** 个合成集成）；仓库外 wheel **111 passed**。公开源码和 diff 检查随提交运行。当时这些 Python 修复尚未替换 Mac mini 的 `7770768`；现场图片准备故障不是入队分支的验收结果，二者不得混同。当前运行版本见页首。

21:12 的首次群文字尝试在发送前返回 `selection_drift`，没有创建 outbox intent。原因是停止的旧 controller 仍保存原稳定 CLI 生成的 selection 指纹，而受管兼容入口会临时替换 `wx_cli`。修复 `4647448` 只允许“除 CLI 路径外配置完全相同”的替代指纹；第二次尝试继续到 takeover 后，发现 takeover 的后续校验仍只接受单一指纹。修复 `5cf1048` 在本次内存操作中传入原稳定指纹集合，但不改写共享 selection 文件，因而稳定入口仍保留原兼容性；不同 consumer、state 或其他配置仍拒绝。

21:15 使用独立构建的 `5cf1048` 候选重试同一群文字意图。之前两次均在 outbox 建立前停止，现场核对不存在该 request ID；最终原请求仅产生一次 attempt、结果为 `ui_observed`。当时群目标恢复 `push/observe`，桌面通道暂停，旧观察进程继续使用固定 `7770768`。[当期 CI 41310258219](https://github.com/cyl19970726/wx-cli/actions/runs/41310258219) 四项成功；仓库外 wheel 111 passed。

实机图片失败后新增了保守的原生回退：先执行原键盘复制；只有剪贴板仍为本次哨兵时，才在当前校验窗口内查找“恰好一张、完整位于 composer 内”的 Accessibility 图片，确认位置稳定后点击并再次 Command-C。成功仍要求一次剪贴板变更、单张 PNG/TIFF 和与冻结源相同的规范化像素。当期 Swift 参数测试、arm64 构建及签名验证通过；该阶段未取得队列图片成功证据。

## 2026-11-02 实机补验与停止点

为避免把观察等待当成剩余功能验收，本轮在约 17 小时 40 分后主动正常停止旧 `7770768` 观察进程；不是完整 24 小时运行。停止前采集无错误、无活跃模型回合，发送保持暂停。随后以原 ID 冷续接两个受管 Session，二者均为 `idle`、`ready`，没有替换 thread。

| 验收项 | 实际结果 | 未闭合边界 |
| --- | --- | --- |
| 私聊、测试群文字 | 各一次 `ui_observed`，用户分别确认收到。 | 无新增阻塞。 |
| 测试群四色合成图片 | 用户在授权测试群独立确认收到；Rust 源记录新增正确的同 talker 出站 Image 事件，cursor 49。 | 这是受控桌面操作产生的实际出站证据，不等于原 Rust dispatch 已自动完成。 |
| 候选原生助手 | Candidate 2 独立 bundle ID 保留；新鲜进程核验 Accessibility、Screen Recording、Post Events 均 true。稳定助手未替换。 | 回退的真实像素回读仍需通过队列驱动的原始请求验证。 |
| 入站 Session | 私聊已有真实事件处理：delivery 1 `skipped`、delivery 2 `deferred`，对应 transport turn 均 completed，原 thread/turn 映射保留。 | 桌面 App 持有的业务 Session 推送和跨宿主推送仍未实现。 |
| 并发与停止 | 群、私聊均 `push/observe`，桌面暂停；手动接管已交还，群 epoch 15、私聊 epoch 3；旧观察进程已正常停止。 | 不宣称 24 小时连续观察通过。 |

原群图片请求在 outbox 仍是 `managed_media_queued`、attempt 0，dispatch 6 为 `blocked/owner_epoch_changed`。配置→恢复阶段改变 epoch 时，零 attempt 恢复按原 owner/epoch 拒绝；它没有被重放、换 ID 或把人工发送冒充为其队列完成。私聊旧图片请求仍 `indeterminate`，没有 submit result，草稿保留；历史 queued focus 也保留。因而图片实机收发已通过，队列自动恢复与原图回退的完整端到端验收尚未全部通过。

### 队列恢复与原生图片停止点（2026-11-02 后续）

`20a24d9`、`a32cbd8` 修复零尝试意图在 owner epoch 变化后的恢复，并为协调器/outbox 两个 SQLite 文件记录精确恢复代际；中断后只可续接该转换。`7e4f798` 使旧代际隔离项不再挡住当前队列；未知项仍阻断所属会话。`d45867d` 让桌面审查预检每个受管 sender，而不是只查第一项。`88a5fc8` 保留旧 sender 兼容并支持候选 Python 启动器。

真实 dispatch 11 被领取后，attempt 1 在聚焦阶段停止，没有 submit。`52438e7` 增加 `abandon-unsent`：只有绑定 attempt 的原生报告明确处于提交前、且没有 submit-attempted 或提交状态文件时，才把 dispatch 标为 `abandoned_pre_submit`；outbox 的 indeterminate 不变。dispatch 11 和后续 dispatch 12 均按该证据归档，没有重放；私聊未知图片仍保留。

`8ca44ce`、`bf0deac`、`a61459a` 分别调整图片复制回退、已知同图草稿核验和菜单 Copy 动作。[当期 CI 41456634689](https://github.com/cyl19970726/wx-cli/actions/runs/41456634689) 四项成功，原生构建与参数检查通过。该阶段 Candidate 3 的实际准备仍报 `Composer Copy did not replace image sentinel exactly once`，没有成功像素回读或 submit。`cd35203` 只增加剪贴板类型诊断；后续候选状态见页首。

因此，共享队列的恢复与真实领取已有证据，**真实图片队列自动完成仍未通过**。桌面 App 持有的业务 Session 推送与跨宿主推送仍未实现；完整 24 小时观察未完成，现已取消本轮等待。

### 非图片功能补验（2026-11-02 后半）

按用户决定，图片发送放到最后，不再阻塞其他功能验收。`4007f02` 诊断证明微信的图片复制同时携带 TIFF 位图和文件 URL/富文本元数据；原生证据改为只使用像素一致的位图，不读取附带的 URL。原生参数与构建检查通过，但尚未再跑完整图片发送，**不据此宣称图片已通过**。

非图片实测结果：

- 两个原受管 Session 在候选代码上均成功冷续接原 thread。后续群真实入站 delivery 4 为 `skipped`，transport turn `completed`，未发送回复；它不受私聊占用影响。
- 新私聊 delivery 3 已持久化为 `pending`，原 thread 报告 `already has an active writer`，适配器拒绝强行续接或创建替代 Session。该入站没有丢失，但本轮未处理完成。
- 两个真实 channel 并发增量读取成功，未见新事件；桌面采集与 Session 分发独立工作。
- 新文字队列 dispatch 15 已实际领取，因输入框中保留的测试图片草稿而在准备阶段停止，attempt 1 `indeterminate`，没有 submit。其未知状态保留，未重放或换 ID。
- 最终桌面发送暂停，未知项保留；没有新的 24 小时观察或常驻定时任务。

这些结果覆盖本机受管 Session、真实入站隔离、并发采集和发送保护。它们不覆盖被桌面/App 占用的私聊 Session 处理、图片成功发送、跨宿主推送或完整连续运行。

## Mac mini 部署与保护

源码按提交直接拉取后在独立候选目录构建，避开不稳定的批量 SCP。当前只为授权测试群与测试私聊启用托管；另一个已登记目标没有被操作。共享 registry、原 controller 配置及三个原稳定入口的字节哈希保持不变。新 CLI 可对已托管、停止的旧 controller 临时使用候选 Rust API，不改写其原命令或 consumer。

启动前核对旧 runner 停止、无 pending、无交互租约和活跃 helper，保存两个授权 state 的 SQLite 备份、派生配置及媒体。只按已知旧内容迁移这两个 state 的生成 driver。启用 owner/input 后，原绑定、consumer 和全部 attempt 保持一致；未恢复旧数据库快照、未删除失败意图、未换 request ID。

新增 Session 的首次创建在 0.166.0-alpha.5.1 上暴露空 thread 无 rollout 的问题。旧失败引用没有改写，也没有业务数据关联。修复后的明确 setup 先保存原引用，完成一次无微信内容的只读初始化，再关闭连接并冷续接同一 ID，之后才标记 ready。两个新的专用测试身份均通过此验证；它们不代表桌面业务 thread 已被接通。细节见 [Session 记录](SESSION_ADAPTER_VALIDATION_20261101.md)。

## 有界观察

以下为已结束的历史观察，时间均为 Asia/Taipei：2026-11-01 **19:52:26** 开始，原计划至 2026-11-02 **19:52:26**。当时进程运行在 Mac mini，SSH 断开后仍存活；健康文件更新、采集无错、Session 无需检查事项、执行器存活。策略为 `input=push / reply_policy=observe`，桌面通道暂停；初始化回合、模型最终文字和工具输出不会转发微信。

此观察覆盖采集、inbox、受管 Session 及后台进程。20:29 的私聊 GUI 测试和 21:15 的群文字测试临时使用 pull/authorized，之后两目标均恢复 push/observe，桌面再次暂停，原观察进程未重启。因此不是单一配置下连续发送暂停的 24 小时记录。11 月 2 日主动正常停止旧观察，定时提醒已按用户要求取消。实际私聊入站产生 skip/defer；这不代替忙闲故障演练或 24 小时稳定运行验收。

## 2026-11-02 GUI 验收与旧停止点

早先原生桌面工具返回 noWindowsAvailable/超时；用户明确同意 AppleScript 后，System Events 返回 osascript 未获辅助访问（-25211），直接应用脚本查询也超时。微信助手自己的三项权限为真，两者不是同一权限主体。浏览器自动审查拒绝 VNC 链接，理由是只允许 HTTP/HTTPS 协议，未通过间接浏览器方式重试。

20:26 原生 Screen Sharing 实际点击恢复：从微信“窗口”菜单选中设置，再关闭其红色关闭按钮。新鲜画面和助手 inspect 均确认只剩主窗口，Accessibility/Screen Recording/Post Events 均 true。没有调整权限、编辑 TCC、重签或替换助手。

20:29 在暂停模型投递（pull）并临时启用已授权测试发送后，使用候选原命令通过共享队列执行：

| 目标/内容 | 结果 | 边界 |
| --- | --- | --- |
| 授权私聊文字 | attempt 1 为 ui_observed，约 7.25 秒 | 中文、换行、emoji 的本地证据；用户独立确认收到，文字和换行正常。 |
| 授权私聊四色合成图片 | attempt 1 为 indeterminate | 原生 prepare 报 `Composer Copy did not replace image sentinel exactly once`；没有 submit result，图片草稿仍在。没有点击另一个发送入口、清草稿、重放或替换 ID。 |
| 授权测试群文字 | 21:15 通过兼容候选完成，attempt 1 为 ui_observed | 中文、换行、emoji 的本地出站证据；用户独立确认测试群已收到。 |
| 授权测试群图片 | 未执行 | 私聊图片仍在准备阶段停止，先用群文字验证迁移后的目标协调。 |

当时两目标策略恢复 push/observe，桌面通道暂停，执行器/采集仍存活，无活跃模型回合或 Session attention。之后旧观察进程已停止；最新 worker 状态见页首。原图片的状态继续按 indeterminate 保留；缺少 submit result 不等于独立收件判断，也不授予图片恢复权限。P0–P4 仍未全部验收通过，不能描述为上线就绪。

## 2026-11-03 当前文字队列证据

在 Mac mini 运行的 `c8f0f0c` 候选完成同群两个不同文字意图：Q2/Q3 各有一次 attempt，均为 `ui_observed`，源事件 cursor 51/52。第二执行器在首项执行期间返回 busy；同时读取授权私聊成功。对已完成 ID 的重复查询没有新增 attempt。用户独立确认顺序、正文、换行和 emoji 正确。

随后两个授权会话并发入队，由单一执行器依次完成 dispatch 22 私聊、dispatch 23 群；两者均为 attempt 1，源事件 cursor 分别为 12 和 54。用户独立确认位置和内容正确，没有串会话。输入法 ABC 指示及 caret 会改变空输入框外观；当前候选只允许每配置一个主引用和最多两个经确认的校准替代图，对整个输入框逐像素匹配，没有遮罩或放宽比较。

接管隔离的独立有界核验也已通过：同目标两个并发 takeover 只有一个 CAS 成功；另一会话可同时读取，绑定不变。原 epoch、接管中当前 epoch、交还前迟到的旧 epoch 发送均被 `owner_epoch_drift` 拒绝，未创建意图或 attempt；旧 epoch handback 也被拒绝。正确 handback 恢复原 owner、`push/observe`、原 Session 引用及 processed cursor/watermark。该目标 epoch 52→54，另一目标 epoch 9 不变；历史 attempt、intent、channel binding、consumer、driver/config 和 dispatch/surface 均保持，桌面效果为零。公开证据索引 E07 对应私有原始记录；此结果不覆盖人工出站检测或监听停止健康。

主 agent 新鲜一次只读 probe 中，私聊原 thread 报 `busy/active writer`，群 thread 为 idle；未调用 `turn/start`，没有向原业务 Session 投递。Mac mini 已安装 Codex 0.166.0-alpha.5.1 的帮助列出 `queue --thread --message [--remote --resume]`，本机 0.164 的帮助也列出该入口。这仅是历史核查线索；新架构不以外部 thread 控制作为 wx-cli 核心路径，现已停止该实现探索。`session_scope` 历史仍正确；`configure` 后 binding 的 `session_ref` 可为 NULL，单独这一字段不能推断换了 Session。

按通道/plugin 分层补验，`c8f0f0c` 候选对仅两个授权 channel 各用独立观察者 consumer 并行执行两次 `channel subscribe --once`：私聊两次各读 12 条、群两次各读 54 条历史事件。事件身份与递增 cursor 均符合绑定；没有 ack，退出再启动仍完整重读，原 consumer 行保持不变；模型调用、ack 和新发送均为零。公开证据索引 E08 对应私有报告。此验证只覆盖底层订阅和不确认时的重读，没有新入站处理，也不证明 Session plugin 接入或跨宿主投递。

以上是有界文字队列、独立收件与接管隔离证据，不覆盖真实图片队列。私聊 delivery 3 仍 pending，原 thread busy 时未换 Session；群 delivery 4 已 `skipped` 且 transport turn completed。两个授权目标已恢复 `push/observe`，桌面发送暂停，无运行中的 worker。完整 24 小时观察已取消，不以等待时间替代功能验收，也不写成通过。

## 2026-11-03 订阅边界与停止状态收口

按用户澄清，通道提供订阅和消息/发送能力，Session 插件消费该能力并使用宿主接口通知原 Session。[订阅与插件合同](CHANNEL_SUBSCRIPTIONS.md)列出当前底层订阅、inbox 接口、进度含义及未交付的插件部分。现有受管 Codex 路径是可选适配实验；本轮未把它改称通用插件，也没有向桌面持有的原 thread 再次投递。

`44fd7c4` 修复监听退出后残留运行健康的问题，并覆盖竞争监听器不得写另一个实例的健康、最终写入必须在释放锁之前、执行器超时与清理失败不得谎称停止。当前 wheel 的 123 项检查含这些回归。`0eb52c0` 收紧 `abandon-unsent`：Rust 在共享锁下核对当前文字 attempt、规范路径、原生状态和 submit 痕迹，复用原 driver 能力握手及完整只读证明，之后以短事务 CAS 归档调度。缺失/矛盾证据仍未知；图片保守拒绝。合成集成和 sender 回归通过，本轮没有拿实际未知请求重新归档或重试。

Mac mini 独立构建安装 `c61ab03` 后，两个授权目标的全部绑定、consumer、outbox/attempt、delivery、dispatch 和 surface 行保持一致，原生助手未重建或替换。随后进行约 5 秒的 collector-only 实测：不传 Session 配置、不启动模型、不发送；两个目标采集无错误。第二监听器被拒绝且未覆盖原实例健康。主进程正常退出码 0，最终 `run_state=stopped`，collector/executor 均不存活、executor 退出码 0；原 delivery/turn、attempt、intent 和 consumer 保留，发送暂停保持。此结果覆盖生命周期，不是新入站业务处理或持续运行成绩。

## 2026-11-03 应用发件、原 Session 恢复与 runner 分阶段检查

`36ec086` 的 [CI 41583448361](https://github.com/cyl19970726/wx-cli/actions/runs/41583448361) 四项成功；Rust 完整测试 **861 passed、11 原有 ignored**，仓库脚本 **130 passed**，最终安装 wheel **184 passed**。`09ea8be` 增加前台通知 runner，已提交推送；本地安装包 **193 passed**、脚本 **130 passed**，Mini Python 3.9 安装包 **193 passed**、脚本 **130 passed**，未重建原生助手；[CI 41588767586](https://github.com/cyl19970726/wx-cli/actions/runs/41588767586) 四项成功。

两个普通应用分别通过强制命令、限定范围且已鉴权的 SSH 通道提交文字。各自只有一次出站 attempt，结果为 `ui_observed`；用户逐项独立确认目标、内容及换行正确且各收到一次。鉴权拒绝、跨范围拒绝、读取拒绝、无 shell 与无 PTY 探针通过。这是**应用发件及安全边界**的有界实机证据，不代表 Session 插件跨宿主投递通过。三个项目 Hook 均已通过宿主正常原生信任流程，不代表 Hook 回调必然完成业务决定。

原测试群的专用 native TUI Session 已收到一条新入站对应的通知，并在原生回合中成功读取该 delivery 冻结的 `show/events` 范围。首次 `complete` 因该旧 Session 持久化的工作区写入范围为空、网络权限关闭而返回 `result_unknown`。对**当前测试 Session**正常调整权限后，操作员沿原 Session、原 delivery ID 与 lease 续接，明确提交 `skip`；Rust 保留最初原生 turn，传输状态最终 `completed`，后续恢复回合亦有终结证据。此项标为**原 Session 恢复成功**，不能写成无人工介入的单回合自动处理成功；未知结果没有通过替换 ID、租约或 Session 清除。私聊旧 pending 仍保留。

其后的全新测试入站由中性采集与新 runner 捕获，在原群专用 native TUI Session 的**同一个实际原生回合**收到精确回调，自动读取冻结的 `show/events` 并成功提交 `skip`；没有操作员续接这次业务决定。独立终态读取随后证实该原生 turn 为 `completed`、宿主仍存活，但 Rust transport 曾把它记为 `interrupted`。**保留原 delivery 的错误传输状态，不直接改数据库，也不据此宣称 P3 完整通过。**图片预检为 ready，但本阶段没有新的图片发送尝试；桌面 App 持有的原业务 Session、实际异机插件投递及真实图片队列仍未通过。完整 24 小时观察已取消，不作为等待条件或验收成绩。

### 原生终态读取修复与独立回归（2026-11-03）

独立诊断在**同一个原 Session** 执行有界的本地等待回合，不读取 inbox，也不操作微信：宿主回合仍活跃时，独立 `thread/read` 返回 `interrupted` 且 `completedAt` 为空，`d2a0dae` 读者保守返回 `unknown`；实际原生回合完成并出现完成时间后，同一读者返回 `completed`。该源码修复已提交推送，本地与 Mini 的仓库脚本各 **130 passed**、已安装 wheel 各 **195 passed**。[CI 41590868934](https://github.com/cyl19970726/wx-cli/actions/runs/41590868934) 四项成功。

Mini 已启用完整 `d2a0dae` 运行时；两个授权目标的全部账本经升级前后逻辑及字节核对保持不变，固定原生助手、应用 gateway 策略和凭据未变。项目 Hook 可执行文件按本安装器范围正常卸载并重装到新版，三个 Hook 已通过宿主正常原生信任审查。旧入站的业务 `skip` 与原生完成证据保留，Rust 历史 `interrupted` 未被手工修正。

在新版运行时上，另一个全新入站经原专用 native TUI Session 自动处理：原生 Hook 精确关联同一回合，模型只读冻结的 `show/events`，各项读取及 `complete` 均成功返回并保存 `skipped`；原 lease 已释放，Rust transport 与原生 turn 最终均为 `completed`，后者有完成时间，全程无操作员恢复。正式私有报告的 **12 项检查均通过**，因此**受管原专用 native TUI Session 路径**取得一次完整实机闭环；此前的恢复案例和误记案例仍各自保留，不以新成功覆盖历史。中性采集与新 runner 的 300 秒有界运行均正常结束、退出码 0，所属子进程已退出；原宿主在完成后按正常流程停止。桌面 App 持有的原业务 Session 与实际不同宿主的 Session 插件投递仍未验收。

不同物理宿主的本地控制端使用仅有 `session_accept` 范围的凭据，经真实受限 SSH 访问 Mini 的 inbox API：可读取上述全新入站的冻结事件；错误私聊会话范围的读取被拒，`send_text` 被 `permission_denied` 拒绝，主机名核对证明确为不同机器。私有实机报告的检查均为 true。这证明**异机只读 inbox API 与范围隔离**，没有在控制端运行 Session 插件、领取或完成该 delivery，不能宣称跨宿主 Session 投递通过。

真实图片队列随后用合成多色 PNG 和配文开始有界尝试。图片 attempt 1 在**原生聚焦、图片准备之前**变为 `indeterminate`，原生静态错误为 `Search popup not enumerated by ScreenCaptureKit within six seconds`；助手三项权限均为 true。聚焦阶段的 23 次检测没有枚举到弹层，失败后只读 inspect 却发现额外的搜索弹层。没有 `mediaPrepared` 或 submit 证据，配文仍为零 attempt 队列项，原意图和元数据保留。GUI 工具点击返回 SCStream 错误，远程 System Events 按键也被拒；这两种工具的失败不等于候选助手的权限失败。仅请求用户按 Esc 关闭已知搜索弹层，GUI 恢复仍在进行。**不能据此判定图片协议失败或图片已发出。**授权群已恢复 `push/observe`，另一授权目标保持原模式，全局桌面发送暂停。

### 图片聚焦失败的限定归档候选（2026-11-03）

用户按 Esc 后已核对只剩一个微信主窗口；随后新 focus 虽排队，领取仍为空，因为原图片 attempt 1 的不明调度继续阻塞。**新 focus 没有执行**，这不是新的桌面聚焦通过或图片复制问题。Rust 与 Python 新增的未部署候选仅允许对该类首次图片 attempt 作原生只读证明：报告必须停在 `inspected`，完整 inspect/focus/polls 必须匹配已知六秒搜索弹层未枚举错误，且无图片准备、状态、提交、后续 attempt 或活跃 helper 证据；Rust 在共享锁下通过独立 `--abandon-capabilities` / `--abandon-preflight` 合同核查。文字原路径不变；任意图片错误、`group_focused` 或已准备图片均不适用。详细命令和边界见[托管会话指南](MANAGED_CONVERSATIONS.md)。

安装候选且原证据核验通过后，操作员命令只会把原调度记为 `done/abandoned_pre_submit`，保留 outbox 的 `indeterminate`、原 intent 和 attempt；它**不恢复或重试图片**，也不自动执行后续队列。本地候选的完整 Rust 测试 **861 passed、11 原有 ignored**，严格 all-target Clippy 和 fmt 已通过；Python 仓库脚本 **136 passed**（其中 sender 55 项，包含 5 项新增图片证明回归；编译 coordinator 集成 20 项），仓库外已安装 wheel **195 passed**，Swift 参数与命名剪贴板检查通过，公开源码检查通过（扫描 326 个索引文件），diff 检查干净。这是源码候选工程证据，原生助手无需重建；**候选尚未部署，真实图片请求尚未归档**，新 focus 未执行，也没有图片成功证据。Mini 分阶段安装后的现场结果另行记录。

### 限定归档已部署及不同图片的新尝试（2026-11-03）

`a9fdce6` 在 Mini 独立构建后已启用，升级前后两个授权目标的全部账本逻辑记录、归属和代际保持不变，原生 Candidate 3 二进制未改。对原群图片 attempt 1 的只读证明及 Rust 正常 `abandon-unsent` 命令均成功：只归档其调度为 `done/abandoned_pre_submit`，原 outbox、attempt 和所有原生证据未改，其余调度未改，也没有发送或重试。首次私有核验脚本误带额外绑定字段而被严格拒绝，修正后的显式合同通过；失败脚本证据保留。

随后原来已排队、从未执行的 focus 实际完成，原生精确目标证明通过；两目标 outbox 均未改变。私有脚本把正常结果标签误写为 `focused`，正常 Rust 实际结果是 `focus_completed`；后续仅作只读核对并保留原脚本失败，没有重跑 focus。

再用**另一张不同图片**与新业务意图执行图后配文的有序队列。图片 attempt 1 已通过精确群聚焦，在粘贴后的复制核验处被拒，错误为剪贴板计数未“恰好增加 1”；没有提交、没有出站事件，配文仍为零 attempt。原状态和草稿保留，此尝试不适用仅限 `inspected` 的准备前归档，不能重放。使用现有媒体解码代码的只读探针确认剪贴板只有一个图片项，五次稳定观察均与本次冻结源的 sRGB 像素完全一致；附带文件 URL 和 Qt 元数据没有被打开或当作图片来源。这说明像素一致，但不构成发送或独立收件证据。图片复制计数的源码修复正在独立验证，未替换已授权助手。

本阶段结束后两目标均为 `push/observe`，群 epoch 60、私聊 epoch 11，全局桌面暂停，无新增自动回复或未知请求重试。异机桌面聊天的接入配置仅已私有准备，未安装 Hook、改变信任、激活凭据或向该聊天发消息，不计入实际异机 Session 验收。

### 真实采集进程停止与重启（2026-11-03）

在桌面暂停、无 Session 通知器的条件下，启动真实中性采集进程，等其取得运行健康后发送正常 `SIGTERM`，随后重新启动同一配置完成一次采集。两次退出码均为 0，最终健康为 `stopped`，所属执行器已退出并由父进程回收。两个授权目标的全部 outbox intent/attempt、已有 delivery、owner/epoch 和 processed cursor 保持不变；私聊原 pending 仍为原条目，没有调用模型或微信发送。这覆盖真实信号停止、健康收尾及同状态重启，不冒充网络断线、微信退出或停采期间新入站的积压恢复。

### 图片 Copy 所有权计数的原生修复候选（2026-11-03）

原生图片 Copy 改为要求本次哨兵已被替换、剪贴板所有权计数前进、单项 PNG/TIFF 的解码像素与冻结源完全一致，并在解码、短暂等待和新鲜目标画面核查期间保持计数稳定；不把所有权计数当成 Copy 动作次数。文字规则未变，图片文件 URL 不会被打开；外部剪贴板变化仍拒绝恢复。相关项目操作 Skill 与原生使用说明已同步。

本机命名剪贴板回归通过，包括正确图片经历两次所有权变化、未变化/倒退计数、未移除哨兵、错误像素、多项、无栅格、仅文件 URL，以及外部剪贴板变化不得覆盖。独立 arm64 编译通过。完整 Rust **861 passed、11 原有 ignored**、fmt、严格 Clippy 与 build 通过；Python 仓库脚本 **136 passed**，仓库外已安装 wheel **195 passed**。一次并发检查中旧的非 deadline fixture 未到达预检捕获步骤；其一般启动预算改为 10 秒，并精确断言原预检拒绝原因，原 30 毫秒超时用例和生产预算不变，完整检查随后通过。

这些是修复候选的工程证据，**不是修复助手实机发送通过**。Mini 已授权的 Candidate 3 保持原二进制；新的原生候选尚未激活，原不明图片、草稿和配文均保留，不因源码修复而重试。

## 私有证据索引

- E01：本地忽略目录中的工程矩阵、完整 CI 日志及公开材料核验。
- E02：远端候选按提交构建、Python 3.9.6 检查和 wheel/binary 摘要。
- E03：授权双目标真实并行空轮询及原 metadata/共享文件哈希对照。
- E04：受管初始化、冷续接、旧失败引用和真实合成 inbox 决定。
- E05：启动前备份、观察 manifest、PID/命令和健康记录。
- E06：助手权限、窗口、AppleScript 失败、后续原生关闭设置，以及文字/图片各一次尝试和停止后的策略恢复证据。
- E07：当前候选文字队列顺序、双会话串行发送、独立收件及接管隔离的脱敏汇总；原始接管核验留在私有验收目录。
- E08：两个授权 channel 的独立观察者并行订阅、退出重启重读及原 consumer 不变的脱敏汇总；原始报告留在私有验收目录。
- E09：当前候选独立构建/安装、原生助手与全账本不变核对、双目标短时采集、竞争监听拒绝和最终停止健康；原始材料留在私有验收目录。
- E10：两普通应用的受限 SSH 独立收件与拒绝探针、三个项目 Hook 信任状态、原专用 Session 的同一租约恢复决定，以及全新入站单回合业务完成与 transport 终态不一致；原生回合、权限和终态诊断原始记录留在私有验收目录。
- E11：同一原 Session 的活跃/完成两阶段只读终态回归，以及 `d2a0dae` 升级前后双目标账本、助手和 gateway 策略对照；原始回合与配置证据留在私有验收目录。
- E12：新版原专用 native TUI Session 全新入站单回合闭环的正式私有验收报告（12 项通过）、三个 Hook 原生信任状态，以及图片队列聚焦停止的脱敏汇总；公共记录不含原始回合、身份或截图。
- E13：不同物理宿主的限定只读凭据经真实受限 SSH 读取 inbox、跨会话读取拒绝及发送拒绝；原始报告留在私有验收目录，不代表异机 Session 插件验收。


- E14：原图片严格准备前归档、原 focus 实际完成及只读脚本核对，之后不同图片在复制核验停止和五次静态像素匹配；原未知尝试、配文和截图均留在私有目录。
- E15：真实采集进程 SIGTERM、同配置重启、最终健康/子进程回收及已有待办/attempt 不变；不包含停采期间新消息积压或网络故障。

真实身份、Session/turn 引用、token、配置、截图与数据库保留在各自宿主的忽略/私有验收目录；公共仓库仅记录上述汇总。
