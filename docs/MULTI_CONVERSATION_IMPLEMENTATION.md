# 多会话实施与验收记录（2026-11-01）

实施候选已交付为 [草稿 PR #6](https://github.com/cyl19970726/wx-cli/pull/6)。当前运行代码为 `777076844fc59376019b85a4cdd42ec2be7aaccb`；本页明确区分实现、离线检查、真实读取、受管 Session 与 GUI/收件证据。原文字的历史验收继续有效，新增功能不继承旧版实机结论。不合并、不发布、不向非测试业务群发送。

## 分批状态

| 批次 | 已有证据 | 未关闭的验收 |
| --- | --- | --- |
| P0 图片/文字基线 | 复用图片 `3c7ba2c`、生命周期及恢复 `86c1db3`，没有重写基础收发。私聊和测试群文字、测试群四色图片均获用户独立收件确认；图片 Rust 源事件为 cursor 49。Candidate 2 三项权限新鲜检查为 true。 | 私聊旧图片原尝试仍 indeterminate、草稿保留；候选回退的队列原生像素回读尚未完成。 |
| P1 会话隔离 | 逐会话锁、显式 ID、目标 controller 协调和 owner/epoch 已实现。Mac mini 两个授权目标同时增量读取，合计 0.086 秒，两页均无新事件；原绑定、consumer、attempt、意图元数据及共享配置均未改动。 | 真实 GUI 并发、接管临界区和人工出站的现场复验仍未完成。0.086 秒是两次空增量轮询，不是历史消息或容量成绩。 |
| P2 共享发送 | 文字/图片/focus 共用 Rust dispatch；包含意图来源代际、补登记、顺序 batch、优先级老化、到期拦截、原 helper 退出保护、未知结果停通道及零 attempt 核查恢复。19 项编译 CLI 合成集成通过。真实队列已领取图片 dispatch，并在原生准备阶段停止。 | 原生像素回读未通过，不能称图片队列成功。 |
| P3 inbox/Session | 持久批次、处理租约、skip/defer/draft/reply、持续待办和决定历史已实现。私聊真实 delivery 1 skip、delivery 2 defer 均有 completed transport turn；两个原 Session 冷续接后 idle/ready。 | 桌面 App 持有的业务 Session 推送及跨宿主推送未接入，原业务 Session 继续主动 CLI。 |
| P4 运行验收 | 最新四任务 CI 通过；Mac mini 独立构建/安装和 Python 3.9.6 矩阵通过。两个测试绑定完成有界实机验收，发送保持暂停，观察正常停止。 | 真并发现场故障注入和 24 小时连续观察未通过；旧观察约 17 小时 40 分后停止。 |

## 工程验证

代码 `7770768` 的 [CI 41302033715](https://github.com/cyl19970726/wx-cli/actions/runs/41302033715)四项成功：Rust、Python 3.9、Python 3.12、Swift。若后续提交仅更新本文，运行版本仍固定于上述代码；最新提交的自动检查以 PR Checks 为准。

| 检查 | 实际结果 |
| --- | --- |
| Rust 完整测试 | 859 passed、11 原有 ignored、0 failed；严格 all-target Clippy、fmt 和 build 通过。 |
| 编译 CLI 合成集成 | 16 passed，含文字/图片并发、GUI 排他、锁忙零 attempt、批次、过期、孤儿意图、本人出站、Session 历史作用域和未知不重放。真实 CLI 指执行编译程序与生成的 SQLite，不是真实微信。 |
| Python CI 脚本 | Python 3.9/3.12 各运行 97 项，2 项编译 CLI 场景跳过；对应场景在 Rust job 实际执行，不把跳过算通过。 |
| 仓库外 wheel | Python 3.9/3.12 各 108 passed，含入口、首次初始化、冷续接前置条件、未知结果不替换引用及 scoped write roots。 |
| Mac mini Python 3.9.6 | 仓库脚本 113 passed；仓库外 wheel 108 passed；Rust 构建和独立候选安装完成。 |
| Swift | 参数与命名剪贴板恢复检查通过。助手未因 Python/Rust 升级而重编、替换或重置权限。 |
| 可分享材料 | 图源与经原通过回执核验的空白规范化公开 HTML 一并保存；公开源码与 diff 检查通过。图稿不是实现或实机证据。 |

早期并发负载曾使原有两秒进程探针超时；完整复跑与最终 CI 已通过，没有降低断言或新增 Rust 忽略项。早期 Session 写权限、未持久化空 thread 的失败证据均保留。单元测试和安装包检查不代替实机收发。

## 后续兼容修复

GUI 验收期间的源码核查发现：`--no-wait` 在冻结新 outbox 意图、检查复用 ID 的正文/图片/调度参数前就尝试 enqueue。已将非阻塞分支移到相同的身份、内容、状态、metadata 和 driver 检查之后，再入队；仍不执行 GUI 或等待结果。两个单元回归覆盖新文字/图片、复用内容/调度冲突及不明尝试不得入队。新增编译 Rust 集成验证真实冻结、两个零 attempt 排队项、源图片删除后冻结字节仍存在，且没有 driver 调用。

本地 Python 3.12 仓库脚本 **114 passed**（含编译 CLI 的 **17** 个合成集成）；仓库外 wheel **111 passed**。公开源码和 diff 检查随提交运行。这些 Python 修复尚未替换 Mac mini 的 `7770768`；现场图片准备故障不是入队分支的验收结果，二者不得混同。

21:12 的首次群文字尝试在发送前返回 `selection_drift`，没有创建 outbox intent。原因是停止的旧 controller 仍保存原稳定 CLI 生成的 selection 指纹，而受管兼容入口会临时替换 `wx_cli`。修复 `4647448` 只允许“除 CLI 路径外配置完全相同”的替代指纹；第二次尝试继续到 takeover 后，发现 takeover 的后续校验仍只接受单一指纹。修复 `5cf1048` 在本次内存操作中传入原稳定指纹集合，但不改写共享 selection 文件，因而稳定入口仍保留原兼容性；不同 consumer、state 或其他配置仍拒绝。

21:15 使用独立构建的 `5cf1048` 候选重试同一群文字意图。之前两次均在 outbox 建立前停止，现场核对不存在该 request ID；最终原请求仅产生一次 attempt、结果为 `ui_observed`。测试结束后群目标恢复 `push/observe`，桌面通道暂停，观察进程继续使用固定 `7770768`。最新 [CI 41310258219](https://github.com/cyl19970726/wx-cli/actions/runs/41310258219) 四项成功；仓库外 wheel 111 passed。

实机图片失败后新增了保守的原生回退：先执行原键盘复制；只有剪贴板仍为本次哨兵时，才在当前校验窗口内查找“恰好一张、完整位于 composer 内”的 Accessibility 图片，确认位置稳定后点击并再次 Command-C。成功仍要求一次剪贴板变更、单张 PNG/TIFF 和与冻结源相同的规范化像素。Swift 参数测试、arm64 完整构建及签名验证通过；该回退尚未替换当前助手，也未取得实机成功证据。

## 2026-11-02 实机补验与停止点

为避免把观察等待当成剩余功能验收，本轮在约 17 小时 40 分后主动正常停止旧 `7770768` 观察进程；不是完整 24 小时运行。停止前采集无错误、无活跃模型回合，发送保持暂停。随后以原 ID 冷续接两个受管 Session，二者均为 `idle`、`ready`，没有替换 thread。

| 验收项 | 实际结果 | 未闭合边界 |
| --- | --- | --- |
| 私聊、测试群文字 | 各一次 `ui_observed`，用户分别确认收到。 | 无新增阻塞。 |
| 测试群四色合成图片 | 用户在“测试微信机器人”群独立确认收到；Rust 源记录新增正确的同 talker 出站 Image 事件，cursor 49。 | 这是受控桌面操作产生的实际出站证据，不等于原 Rust dispatch 已自动完成。 |
| 候选原生助手 | Candidate 2 独立 bundle ID 保留；新鲜进程核验 Accessibility、Screen Recording、Post Events 均 true。稳定助手未替换。 | 回退的真实像素回读仍需通过队列驱动的原始请求验证。 |
| 入站 Session | 私聊已有真实事件处理：delivery 1 `skipped`、delivery 2 `deferred`，对应 transport turn 均 completed，原 thread/turn 映射保留。 | 桌面 App 持有的业务 Session 推送和跨宿主推送仍未实现。 |
| 并发与停止 | 群、私聊均 `push/observe`，桌面暂停；手动接管已交还，群 epoch 15、私聊 epoch 3；旧观察进程已正常停止。 | 不宣称 24 小时连续观察通过。 |

原 `wx-multi-20261102-group-image-candidate2-v1` 在 outbox 仍是 `managed_media_queued`、attempt 0，dispatch 6 为 `blocked/owner_epoch_changed`。配置→恢复阶段改变 epoch 时，零 attempt 恢复按原 owner/epoch 拒绝；它没有被重放、换 ID 或把人工发送冒充为其队列完成。私聊旧图片 `wx-multi-20261101-private-image-v1` 仍 `indeterminate`，没有 submit result，草稿保留；历史 queued focus 也保留。因而图片实机收发已通过，队列自动恢复与原图回退的完整端到端验收尚未全部通过。

### 队列恢复与原生图片停止点（2026-11-02 后续）

`20a24d9`、`a32cbd8` 修复零尝试意图在 owner epoch 变化后的恢复，并为协调器/outbox 两个 SQLite 文件记录精确恢复代际；中断后只可续接该转换。`7e4f798` 使旧代际隔离项不再挡住当前队列；未知项仍阻断所属会话。`d45867d` 让桌面审查预检每个受管 sender，而不是只查第一项。`88a5fc8` 保留旧 sender 兼容并支持候选 Python 启动器。

真实 dispatch 11 被领取后，attempt 1 在聚焦阶段停止，没有 submit。`52438e7` 增加 `abandon-unsent`：只有绑定 attempt 的原生报告明确处于提交前、且没有 submit-attempted 或提交状态文件时，才把 dispatch 标为 `abandoned_pre_submit`；outbox 的 indeterminate 不变。dispatch 11 和后续 dispatch 12 均按该证据归档，没有重放；私聊未知图片仍保留。

`8ca44ce`、`bf0deac`、`a61459a` 分别调整图片复制回退、已知同图草稿核验和菜单 Copy 动作。最新 [CI 41456634689](https://github.com/cyl19970726/wx-cli/actions/runs/41456634689) 四项成功，原生构建与参数检查通过。Candidate 3 的实际准备仍报 `Composer Copy did not replace image sentinel exactly once`，没有成功像素回读或 submit。`cd35203` 只增加剪贴板类型诊断，仍需该版本重新授权后实测。

因此，共享队列的恢复与真实领取已有证据，**真实图片队列自动完成仍未通过**。桌面 App 持有的业务 Session 推送、跨宿主推送、真并发故障演练和完整 24 小时观察也仍不通过。

## Mac mini 部署与保护

源码按提交直接拉取后在独立候选目录构建，避开不稳定的批量 SCP。当前只为授权测试群与测试私聊启用托管；另一个已登记目标没有被操作。共享 registry、原 controller 配置及三个原稳定入口的字节哈希保持不变。新 CLI 可对已托管、停止的旧 controller 临时使用候选 Rust API，不改写其原命令或 consumer。

启动前核对旧 runner 停止、无 pending、无交互租约和活跃 helper，保存两个授权 state 的 SQLite 备份、派生配置及媒体。只按已知旧内容迁移这两个 state 的生成 driver。启用 owner/input 后，原绑定、consumer 和全部 attempt 保持一致；未恢复旧数据库快照、未删除失败意图、未换 request ID。

新增 Session 的首次创建在 0.166.0-alpha.5.1 上暴露空 thread 无 rollout 的问题。旧失败引用没有改写，也没有业务数据关联。修复后的明确 setup 先保存原引用，完成一次无微信内容的只读初始化，再关闭连接并冷续接同一 ID，之后才标记 ready。两个新的专用测试身份均通过此验证；它们不代表桌面业务 thread 已被接通。细节见 [Session 记录](SESSION_ADAPTER_VALIDATION_20261101.md)。

## 有界观察

时间均为 Asia/Taipei：2026-11-01 **19:52:26** 开始，计划至 2026-11-02 **19:52:26**。进程运行在 Mac mini，SSH 断开后仍存活；现已核对健康文件更新、采集无错、Session 无需检查事项、执行器存活。策略是 `input=push / reply_policy=observe`，桌面通道暂停；初始化回合、模型最终文字和工具输出不会转发微信。

此观察覆盖采集、inbox、受管 Session 及后台进程。20:29 的私聊 GUI 测试和 21:15 的群文字测试临时使用 pull/authorized，之后两目标均恢复 push/observe，桌面再次暂停，原观察进程未重启。因此不是单一配置下连续发送暂停的 24 小时记录。11 月 2 日主动正常停止旧观察，定时提醒已按用户要求取消。实际私聊入站产生 skip/defer；这不代替忙闲故障演练或 24 小时稳定运行验收。

## 最新 GUI 验收与停止点

早先原生桌面工具返回 noWindowsAvailable/超时；用户明确同意 AppleScript 后，System Events 返回 osascript 未获辅助访问（-25211），直接应用脚本查询也超时。微信助手自己的三项权限为真，两者不是同一权限主体。浏览器自动审查拒绝 VNC 链接，理由是只允许 HTTP/HTTPS 协议，未通过间接浏览器方式重试。

20:26 原生 Screen Sharing 实际点击恢复：从微信“窗口”菜单选中设置，再关闭其红色关闭按钮。新鲜画面和助手 inspect 均确认只剩主窗口，Accessibility/Screen Recording/Post Events 均 true。没有调整权限、编辑 TCC、重签或替换助手。

20:29 在暂停模型投递（pull）并临时启用已授权测试发送后，使用候选原命令通过共享队列执行：

| 目标/内容 | 结果 | 边界 |
| --- | --- | --- |
| 授权私聊文字 | attempt 1 为 ui_observed，约 7.25 秒 | 中文、换行、emoji 的本地证据；用户独立确认收到，文字和换行正常。 |
| 授权私聊四色合成图片 | attempt 1 为 indeterminate | 原生 prepare 报 `Composer Copy did not replace image sentinel exactly once`；没有 submit result，图片草稿仍在。没有点击另一个发送入口、清草稿、重放或替换 ID。 |
| 授权测试群文字 | 21:15 通过兼容候选完成，attempt 1 为 ui_observed | 中文、换行、emoji 的本地出站证据；用户独立确认测试群已收到。 |
| 授权测试群图片 | 未执行 | 私聊图片仍在准备阶段停止，先用群文字验证迁移后的目标协调。 |

两目标策略已恢复 push/observe，桌面通道暂停，执行器/采集仍存活，无活跃模型回合或 Session attention。原图片的状态继续按 indeterminate 保留；缺少 submit result 不等于独立收件判断，也不授予图片恢复权限。P0–P4 仍未全部验收通过，不能描述为上线就绪。

## 私有证据索引

- E01：本地忽略目录中的工程矩阵、完整 CI 日志及公开材料核验。
- E02：远端候选按提交构建、Python 3.9.6 检查和 wheel/binary 摘要。
- E03：授权双目标真实并行空轮询及原 metadata/共享文件哈希对照。
- E04：受管初始化、冷续接、旧失败引用和真实合成 inbox 决定。
- E05：启动前备份、观察 manifest、PID/命令和健康记录。
- E06：助手权限、窗口、AppleScript 失败、后续原生关闭设置，以及文字/图片各一次尝试和停止后的策略恢复证据。

真实身份、Session/turn 引用、token、配置、截图与数据库保留在各自宿主的忽略/私有验收目录；公共仓库仅记录上述汇总。
