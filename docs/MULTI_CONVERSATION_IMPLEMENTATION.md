# 多会话实施与验收记录（2026-11-01）

本轮按 [多会话方案](MULTI_CONVERSATION_PLAN.md)推进。以下将代码、离线验证、Mac mini 现场与独立收件分开记录；旧版文字验收不能替代新增协调器的验收。授权范围为测试群和测试私聊、受控升级、fork 分支提交与审阅 PR；不合并、不发布、不操作业务群。本页不包含私有身份、消息或截图。

## 分批进度

| 批次 | 当前进展 | 尚未达到的验收条件 |
| --- | --- | --- |
| P0 图片与 helper 基线 | 图片代码与 helper 生命周期、恢复修复已整合。Mac mini 新鲜核验的 helper 为 `9f`，三项桌面权限均为真，旧 runner 未运行。 | 新集成版本尚无 GUI 图片或文字发送，也无独立收件证据；桌面控制故障仍阻断实机回归。 |
| P1 会话隔离与主动 CLI | 注册表锁缩至快照和更新；逐会话锁、显式 ID、目标 controller 接管、owner/epoch 栅栏及配置命令已有代码和合成回归。 | 双群及私聊的真实并行读取、接管与发件仍待现场对照。 |
| P2 统一发送 | Rust 保留 channel outbox 与冻结意图，新增持久 dispatch、单桌面执行器、文字/图片及 focus 入队、未知结果停通道和重启对账。`batch_id + part_index` 冻结顺序、优先级老化与到期拦截、未关联本人出站判别及其代际暂停已进入代码；16 项真实 CLI 合成集成已通过。 | 新协调器的真实 GUI 发送、人工出站现场识别及独立收件尚未闭环；合成通过不等于 P2 实机验收通过。 |
| P3 inbox 与 Session | Rust inbox、处理结果及持续待办已有代码；连续事件经 1 秒静默、最长 5 秒采集窗口合并，每批上限 256。defer 按到期或新信息重新待办，draft 不自动续办，取消与决定历史保留。当前 owner 绑定保存 `session_ref`，阻止仅改私有配置就替换 thread；显式 `configure` 会更新代际并清除旧引用。托管 inbox 监听与桌面执行器分进程，已有记录的 turn 在重启后按原证据恢复。专用受管 Codex app-server 测试 thread 已验证续接，并在显式限定可写目录后，由同一 thread 的模型通过 Rust inbox 对合成消息成功记录 `skip`；早期只读及中断记录仍保留，详见 [Session 适配器记录](SESSION_ADAPTER_VALIDATION_20261101.md)。 | 真实微信增量到业务处理的连续链路、桌面 App 正持有的业务 Session 投递、跨主机投递及重启故障现场验收均未完成。测试 thread 不代表业务 Session 已接通。 |
| P4 运行验收 | Rust 全量复跑、严格 Clippy、Python 3.12 scripts、已安装 wheel、真实 CLI 合成集成和 Swift 参数检查已有本地通过证据。 | Python 3.9 CI、远程交付、现场迁移/回退与故障演练、新版本 GUI 发送及独立收件、24 小时有界运行和最终审阅仍待完成；24 小时观察尚未启动。 |

新绑定默认 `observe`；只有明确授权的会话才配置 `authorized`。托管发送在冻结意图和入队时携带原 owner/epoch，旧 Session 不得取新代际替旧任务发送；无来源代际的旧排队意图保持阻断。Rust 保存绑定、事件、消费、inbox、意图和尝试；Python 编排；Swift 执行受控桌面动作。`ui_observed` 是本地观察，不是收件证明。

## 离线验证证据

私有原始日志位于忽略目录 `.local/multigroup-evidence/`；此处只记录汇总，不把运行材料加入仓库。测试数来自相应日志或 [Session 适配器记录](SESSION_ADAPTER_VALIDATION_20261101.md)，均为各次执行时的工作区快照；共享代码仍在集成，不把过滤或忽略项算作通过。

| 检查 | 实际记录 |
| --- | --- |
| Rust outgoing 定向测试 | 25 通过、0 失败、0 忽略；`outgoing-tests.log`。 |
| Rust coordinator 定向测试 | 最新 `coordinator-tests.log` 记录 5 通过、0 失败、0 忽略；此前记录为 3 项。 |
| 真实 CLI 合成集成 | `multi-integration.log` 记录 16 通过，覆盖混合文字/图片排队、batch 顺序与失败前项、优先级与到期、defer 续办、人工出站及 owner/Session 栅栏。这里的“真实 CLI”指执行编译的 CLI 与合成状态，不是操作真实微信。 |
| 托管 Python 定向测试 | `managed-python.log` 记录 52 项通过、0 失败；为该次执行快照。最终 scripts 与安装包结果另列。 |
| Python scripts | Python 3.12 运行 113 项通过；`python-scripts-312.log`。此前并发运行时一项 lost-response 测试超时；单独重跑 1 项通过，分别见 `python-baseline.log`、`python-lost-response.log`。 |
| Swift | 参数与 named pasteboard 恢复检查通过；`swift-parameters.log`。不代表真实 GUI 点击或收件。 |
| Python wheel | `wheel-tests.log` 记录已安装 wheel 的入口及 100 项测试通过；此前 89/90 项是较早快照。针对私有状态目录的写权限测试修正后取得本次结果。Python 3.9 本地未运行，可由后续 GitHub CI 核验。 |
| Rust 全量与构建 | `rust-full.log` 复跑退出码为 0；46 个测试汇总合计 859 通过、11 项原有忽略、0 失败。`clippy.log` 记录严格 Clippy 通过；`rust-build.log` 和 `fmt.log` 留有构建及格式检查记录。忽略项不计入通过。 |

此前 Rust 全量首跑的旧恢复用例曾在并发负载下遇到 2 秒探针超时；最新全量复跑已通过，旧失败记录不删除。受管 Session 的早期写入失败也保留在适配器记录中；随后对**同一**测试 thread 显式授予合成状态目录写权限，模型完成 inbox `skip`，Rust 记录原 thread/turn 关联。此证据不涉及微信消息或发送。没有远程 CI 通过或新部署的证据。

## Mac mini 现场与后续闭环

本轮现场反馈：新鲜 helper `9f` 的 Accessibility、Screen Recording、Post Events 三项权限均为真，runner 为停止状态；微信主窗口与设置窗口同时存在。CUA 控制先后返回 `noWindowsAvailable`、超时和 `cgWindowNotFound`，重置后仍未恢复。DERP 路径上的远程 SSH 不稳定；SCP v1 小文件曾送达，但 v2/v3 与二进制均未核验，不能据此认定远端代码或可执行文件已更新。当前没有新的 GUI 发送、图片内容核验、独立收件确认，也没有部署新 coordinator、迁移远端协调器根目录或启动 24 小时观察。桌面控制由执行 agent 继续自查；不把权限为真推断为窗口控制已可用。

后续先完成远程交付核验；经授权推送后可考虑用 GitHub CI 补 Python 3.9，再在远端核对 git clone 与实际安装版本，不能以传输命令退出码代替文件和运行状态核验。执行 agent 继续自行排查窗口控制，核对 helper、桌面锁和目标身份；恢复后才在授权测试会话内做文字与单图各一次的完整出站及独立收件回归。随后验证两个会话并发、接管竞态、人工出站识别、未知结果停通道、重启后的原 turn 恢复、旧状态迁移与回退，再进行有界持续运行。未取得这些现场证据前，状态仍是实施候选，不能标为 P0–P4 验收通过或可上线。

代码基线 `a07e2d1` 已含图片与 helper 恢复整合；本页后续进度以当前共享工作区的未合并候选和上述日期证据为准。私有日志及远端现场材料继续留在忽略目录，不复制进公开报告。
