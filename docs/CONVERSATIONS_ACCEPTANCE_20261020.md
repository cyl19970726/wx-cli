# 按需会话 CLI 现场验收 · 2026-10-20

## 2026-10-21 控制端接管：统一源码与验收边界

用户要求控制端接管后续开发。两个远端开发会话已收到停止变更的通知，后续由单一集成分支 `codex/takeover-reliability` 承接：`8e26359` 的激活、空框、中文按钮和搜索分类修复，与 `60305d1` 的终止换行拒绝规则原来分别以 `d8f4910` 为父、互不包含；现已整合。产品代码自动合并，文档冲突按以下证据边界解决。

| 范围 | 已核事实 |
| --- | --- |
| 既有可用基线 | 10 月 16 日单群接管/回复/交还已实测；10 月 20 日 `d8f4910` 完成一次私聊→测试群往返且两条消息有独立收件确认，见[历史验证](CONVERSATIONS_VALIDATION_20261016.md)。 |
| 新增输入修复 | 终止 LF/CR/CRLF 在新意图入队前拒绝；内部换行、中文与 emoji 保留，历史不确定意图不改写。 |
| 新增运行修复 | 集成 CI 复现子进程管道就绪后仍可能返回 `EAGAIN`，此前会使 Agent 调用失败。读取/写入现在在原时限内继续等待，不重新启动命令，不放宽输出上限。 |
| 后续原生候选 | 搜索分类、进程激活与空框参考图是后续修复；旧版本完成证据不等于该候选通过实机验收。 |
| 接管现场快照 | 原运行时 `d8f4910` 的私聊和群只读读取均通过，每次约 0.28 秒；26 条完整 outbox、consumer 40、原 binding 均保留，runner 停止，无 pending/lease。 |

接管时 fixed-path helper 的 fresh inspect 为 Accessibility、Post Events、Screen Recording 全 false；屏幕共享可显示画面，但坐标操作返回 `noWindowsAvailable`，重连入口操作又超时。该事实只说明当前桌面控制受阻，不能推断微信风控或消息读取失效。后续升级与现场验证结果追加在本节，不以历史授权或截图代替程序权限检查。

统一候选随后已受控安装：复用下文 `5c7db616…` 原生构建，不重新编译或重签；Python wheel SHA-256 为 `3db1e5a1c4175811a2b221139affcfc6bc65216ec0571ea7c0ffa6df8e18a50c`，11 个安装模块与集成源码相同。四锁内保留 app/路由备份并演练代码回退，全部 26 条 outbox、绑定和消费进度保留，未用状态备份覆盖运行状态。已安装新版的私聊/群读取分别为 0.280/0.279 秒，两个入口均在入队前拒绝末尾换行，outbox 没有增加；runner 保持停止，无 pending/lease。安装后的 fresh inspect 三权限仍为 false，因此未进行新聚焦或发送；固定该二进制等待正常系统权限恢复，不能将本次读取成功写成新版收发验收通过。

通过 Mac mini 本机 Computer Use 正常移除并重新加入录屏条目后，fresh inspect 曾为 Screen Recording=true、Accessibility/Post Events=false；可见一个窗口、onConsole=true，配置中的旧 PID 尚未刷新。辅助功能移除需要系统认证。用户明确允许使用现有凭据后，工具仍以安全原因拒绝访问 `com.apple.LocalAuthenticationRemoteService`，没有绕过该限制。这是当时的工具阻断，不是当前仍缺业务授权。

### 权限恢复后的新候选实测

用户随后完成认证；重新检查正常系统设置，发现当前 helper 的辅助功能条目已存在但开关关闭。只打开一次现有开关后，fresh inspect 的 Accessibility、Post Events、Screen Recording 均为 true，没有再次移除重加或触发认证。核对原安装路径、原账号 source 和唯一 880×640 窗口后，仅更新三份私有 sender 配置的当前 PID。权限状态必须以 fresh inspect 为准，不能规定所有 false 都必须移除重加。

统一候选的私聊选择/聚焦分别用时 0.198/2.940 秒，成功取得 kind-aware 搜索证据。首个新测试意图在 `prepare-attempted` 阶段以 `Copied composer text does not exactly match expected draft` 停止，未提交。新截图证实空输入框显示微信的语音输入提示，运行配置未启用已有的空框参考功能。校准整块输入区域的私有 PNG 并固定 SHA-256 后，按既有 `recover-unsent-send` 契约保留原 request ID、正文和 attempt 1；只读 preflight 与 UI 锁内二次核验证实无 submit，才领取 attempt 2。

attempt 2 成功准备中文、内部换行和 emoji，复制回读与原文完全相同；随后在点击前以 `Unique Send label not confirmed at button point` 停止。原生 state 保持 `prepared`，未写入点击提交标记。outbox 仍按保守契约保留 `indeterminate`，且因已进入 submit 作业不允许再次恢复、换 ID 重放或手动发送这份草稿。该结果证明输入准备通过，不证明消息送达；原 26 条历史记录未重放。

之后通过正常 CLI 从私聊切回测试群，选择/聚焦分别为 0.191/3.466 秒；结构化增量读取 0.279 秒，返回 cursor 41–45 共 5 条事件且无更多分页，其中 cursor 45 是入站确认消息。读取不推进 consumer，不为确认消息另发回复；私聊测试草稿保留。

缓存截图的离线 Vision OCR 定位按钮误拒绝：旧 96×46 区域同时读出邻近语音图标的字符和精确的发送标签，故唯一性检查拒绝；不是点击点落在错误按钮。修复为以原校准点为中心的 68×34 区域，保留下方面板与按钮区域两层唯一精确标签要求，并检查标签中心距点击点水平不超过 16px、垂直不超过 10px。没有放宽为文字包含或模糊匹配。

合成回归覆盖邻近图标、重复标签、扩展标签、错位与越界；在隔离副本中仅恢复旧裁剪几何后，源码成功编译，但邻近图标不得进入区域的断言实际运行失败，修复版通过。Swift 完整 typecheck、隔离构建与严格签名通过，新 helper SHA-256 为 `e94a36bd3f89a7821a54c9e4f021e8fd375705c64b9ded6dd47fdf70d6e017e9`。完整 Rust/Python 矩阵也再次通过，计数同下文；此次证据在 `.local/takeover-ci/send-button-20261021/` 与 `.local/takeover-deploy/send-button-candidate/`。这些离线证据尚不证明修复版实际发送成功；后续安装与实测另记。

### 按钮修复版完成有界私聊／群聊实测

源码 `de15fde` 的修复版已受控安装到同一固定路径。升级前停止 writer、备份 app 与私有配置，在 registry/controller/runner/UI 四锁内仅替换原生 app 和三份配置中的 helper digest；Python 包、空框参考图和全部 27 条原发件记录保持原样。新签名的三项权限最初失效，现有开关不能恢复；通过正常系统设置移除无效条目、重新加入当前固定 app 后，fresh inspect 三项全部为 true，当前 PID 匹配。这次没有再次要求用户操作认证，也没有修改 TCC 数据库。

| 新版现场步骤 | 结果与证据 |
| --- | --- |
| 测试群新发送 | 中文、内部换行、emoji 正文完整匹配；端到端 14.435 秒，attempt 1，`ui_observed`，精确目标出站 cursor 46；用户从独立账号确认收到且只有一条。 |
| 私聊新发送 | 只允许与本轮助手留下的已知测试草稿完全相符时替换，未发送旧正文；新的独立按钮验证意图耗时 9.174 秒，attempt 1，`ui_observed`，精确目标出站 cursor 7。用户确认收到且只有一条；原失败意图及两个 attempt 逐字段保持相同，临时 expected-draft 配置已恢复为空。 |
| 自动切回测试群 | 正常 CLI 选择 0.191 秒、聚焦 3.146 秒；随后读取 0.283 秒，精确的新出站消息仍存在且只有一条。私聊与群标题、对应新气泡均由保存截图交叉核验。 |
| 最终状态 | 共 29 条 outbox，升级前 27 条完全保留；两条新成功意图各一次尝试。群 consumer 仍为 40，latest cursor 46；runner 停止，无 pending/lease。未因入站确认消息另发回复。 |

两次新发送都出现过一次瞬时加密副本同步失败，后续在同一次只读观察中恢复并匹配出站记录，**没有再次发送**。图形提交后的记录核验分别约 1.30 秒；端到端耗时含窗口导航、草稿保护和发送前核验，不能只用点击耗时代表整体效率。

修复提交 `de15fde` 的远程 run `39579943446`，Rust、Python 3.9、Python 3.12、Swift 四项均通过。该候选的本轮授权目标、有界自动切换、输入、发送、源记录核验与独立收件已通过；不代表其他客户会话、多常驻 Agent 或长期无人值守已经验证。原始图片、作业、发件记录和收件确认仅保留于私有 `takeover-20261021/live/` 及原生 UI 证据目录，未发布到 GitHub。

### 2026-10-21 集成源码离线验证

集成提交 `8d5a35e` 包含上述两项修复和子进程非阻塞管道遇到 `EAGAIN` 时继续等待的修复。首次本机 Rust 默认并行测试在 `serve-media` 健康请求处挂起，串行 841 项通过不能代替该结果。随后针对测试夹具修复：健康读写受原 10 秒期限约束、要求 HTTP 200，子进程日志落入合成 fixture 文件以避免无人消费管道，失败时清理进程并报告至多 4 KiB 日志；新增无响应 socket 回归。另一项 CAS 竞态测试将启动等待与测试专用 probe 时限协调，在提前失败时清理夹具，并要求具体的过期 claim 错误，保留单次执行断言。没有更改业务超时、关闭测试或串行化 CI。

修复后默认并行 `cargo test --locked` 的 46 个 suite 共 842 通过、0 失败、11 项原有忽略；Rust 格式、全目标 Clippy（警告拒绝）、build、合成 channel（含 1,100 条积压）与编译后 sender 契约均通过。初次无 HTTP 响应的底层触发原因未单独证明，不将测试夹具修复推断为微信客户端故障修复。

Python 3.9.6 与 3.12.8 的脚本测试各 82 通过；两版重新构建 wheel 后，源码外独立安装的包测试各 54 通过，均无失败或跳过。Swift 原生参数测试及主源码 typecheck、公有源码检查和 `git diff --check` 通过。实际命令、版本、首次失败与修复后日志保存在忽略目录 `.local/takeover-ci/`。提交 `bd07749` 的远程 run `39575523323` 已核验 Python 两版、Rust 和 Swift 四项全部通过；后续提交的远程状态以相应 PR 检查为准。工程验证不证明 GUI 送达；权限恢复和后续实测按上文记录判断。

以下是各次试验的历史记录。“未完成往返”均限定当次候选，不抹掉 10 月 20 日已经完成的基线。

## 2026-10-21 历史：聚焦试验失败后回退，分类边界修复仅离线通过

用户完成本机认证后，固定路径助手的既有 Accessibility、Screen Recording、Post Events 权限重新核对为 true。唯一主窗口为原 880×640，原安装路径的当前进程打开原授权 source；两个目标的精确元数据各有唯一候选，与原 kind、稳定 ID、talker 和名称相同。三份 sender 配置仅更新当前 PID，其他绑定不变。

本次配套激活候选 helper SHA-256 为 `6d736d62e9c9fc450c08e248762b0eacaec8e83b0b93c006a04b7bb1ab378351`，Python wheel 的 11 个已安装模块与源码逐字节相同。安装在 registry/controller/runner/UI 四锁内完成，保留旧 app、10 份入口/路由/配置和状态备份，先在隔离目录演练文件字节与权限恢复。本轮不配置空框参考图、不发送、不回复、不恢复原请求。

仅执行一次私聊 focus：起始 inspect 显示微信在后台，候选通过前台与窗口检查后进入搜索；13 次观察均捕获同一搜索弹窗，但没有选出唯一类别匹配结果。8.414 秒后命令退出 1，原生错误为 `Search popup had no unique category-eligible exact target within six seconds`。未点击结果行，没有成功 focus evidence，更未进入 prepare/submit。按试验停止条件，没有调用测试群 focus 或重复私聊 focus。窗口仍显示正常聊天列表及本次搜索词，此结果不证明此前白屏或窗口消失问题已解决。

随后只回退 app/配置/路由：恢复 `d8f4910` Python 环境、`e72ea65e…` 助手和 10 份原文件，未还原状态快照。安装前后及回退后全部 26 条完整 outbox 相同，原意图四个 attempt 保留，群 consumer cursor 仍为 40；channel 绑定和状态目录文件清单相同。runner 停止，无 pending/lease 或助手进程。回退不刷新权限，旧 PID 配置也已恢复；任何后续桌面验收必须重新核对权限、原来源、PID 和窗口。源码提交与运行时升级、现场验收分开；提交这些修复不改变当前回退状态，也不代表验收通过。

### 缓存截图定位与分类边界修复

只对已保存的搜索弹窗做离线 OCR：真实目标位于已识别的“最常使用”分类；较下方“聊天记录”标题被误读，其后的同名历史条目继承了上一个已识别类别，形成第二个候选。没有搜索超时加长、目标模糊匹配或白屏根因结论。

新修复将与已识别分类标题左边缘对齐的未知文字作为关闭分类的边界；对齐范围为两行较小字高的一半。未知标题只能关闭，不能打开可选类别。精确目标、精确类别、80px 距离限制、跨类别重名拒绝及点击前复核全部保留。原有重复/远距条目拒绝断言未改动。新增合成回归先在旧代码中失败，修复后验证：首个合法结果可与未知分类下的历史条目区分，未知分类不能单独授权结果，缩进正文不能伪装分类边界，未知标题下的近距同名条目也被拒绝。相同缓存 OCR 候选在旧实现被拒绝，新实现能选出唯一合法目标；这不代替新的现场聚焦。

再次执行 `.github/workflows/ci.yml` 的完整矩阵，实际命令与下文相同，日志目录改为 `.local/live-reliability-20261021/popup-boundary-matrix`，隔离 candidate 位于其 `candidate/`。Rust fmt/Clippy/build、1,100 条合成积压/去重/重启/source 不变检查、编译后 sender 契约 1 项均通过；Rust 46 个 suite 合计 841 通过、0 失败、11 项原有忽略。Python 3.9.6 和 3.12.14 各 scripts 81 通过、源码外已安装 wheel 测试各 50 通过，无失败/跳过；Swift 参数/命名剪贴板、完整 typecheck、独立构建及严格签名均通过。工具版本与下文激活矩阵相同。

新 helper SHA-256 为 `5c7db616bcc8bd0f8772bb44860189b504ee5ff83aa3f4d8d829e5db06cfa5d2`，仅构建、未安装或运行。11 个活动入口/配置/helper 文件哈希仍为回退基线，26 条 outbox 保持相同。当前没有新发送、收件确认或完整往返验收结论；原意图的第四个 attempt 继续禁止恢复或换 ID 重放。

## 2026-10-21 历史：运行时回退，暂停桌面验收

用户反馈重新登录后前台微信窗口突然不可见。为隔离本轮部署的影响，已停止所有 focus/send/reply 测试，仅做只读进程、签名、日志与状态核对；未启动测试助手。随后使用已演练的回退流程恢复本轮开始时记录的 `d8f4910` Python 环境和 `e72ea65e…` 签名助手，恢复 10 份入口/配置。11 个旧环境模块与该 HEAD 逐字节相同；全部 26 条 outbox 记录、四个 attempt、原绑定和群 consumer cursor 完整保留，未还原状态快照。runner 停止，无 pending/lease，无助手进程。候选源码仍留在工作区，尚未提交或 push；下文新修复的安装描述是 10 月 20 日的历史状态，现已撤回其运行时部署。

只读核对发现安装目录为微信 4.1.13，挂载安装盘另有 4.1.15；两者签名校验通过、签名 Team 相同，共用 bundle ID，按该 ID 选择应用返回歧义。主进程实际来自安装目录，并非两个主进程同时运行。对应系统日志显示前台转为后台，尚未发现主进程退出记录或本轮系统崩溃报告；这些事实不证明窗口问题的原因。未改动、替换或重签微信客户端，也未更改原消息数据库。当前等待用户手动点击 Dock 后的窗口表现，不能将未证实的安装副本冲突当作根因。

回退没有重新刷新 helper 权限或操作微信窗口。旧配置保留原校准 PID；再次 GUI 验收前必须重新确认原账号来源、当前 PID、窗口和权限。原意图的第四个 attempt 仍是不可恢复的 focus 失败，禁止重试、换 ID 重放或通过回退清除。当前没有新发送、收件或完整往返的验收结论。

### 手动窗口恢复与离线激活候选

用户随后确认：点击 Dock 后聊天窗口正常出现。该报告确认手动打开正常，不证明之前消失的原因，也不代表 CLI 验收通过。桌面自动化继续保持停止，活动运行时仍是上述回退基线。

离线审查发现 Python 的 focus/send 路径先用 `open -a` 向安装路径发送应用打开事件。新候选删除这两个调用，将激活放进已有 Swift `focus-group`：先核对权限、console、预期 PID 与窗口 ID/frame，再向同一运行进程请求一次激活；最多等待两秒，随后重新枚举并核对进程、窗口及实际前台，再截图和进入搜索。已经在前台时不请求激活。focus marker 先于激活/输入创建；拒绝、身份变化或前台未建立时停止。协议字段、Rust 状态和 outbox 契约不变。原生 API 请求可能被系统拒绝，不能把其返回值当作实际前台证明，参见 [Apple 文档](https://developer.apple.com/documentation/appkit/nsrunningapplication/activate%28options%3A%29)。operator-only draft probe 也移除打开应用的调用，并要求事先处于前台。

新增回归先在旧代码中捕获六个子场景的应用打开调用，再验证候选：慢 focus 仍可完成；激活被拒绝时没有 prepare/submit；原 attempt 和重复请求证据不变；后台 draft probe 只做 inspect 后停止。当时新候选尚未安装或运行；后续仅聚焦试验与回退结果见本文顶部，不能声称解决了此前白屏或窗口消失。

再次运行下文完整 CI 矩阵和隔离构建，实际日志目录为 `.local/live-reliability-20261021/activation-matrix`，candidate 目录为其 `candidate/`。命令形状、两个 Python 环境和工具版本与下文相同，实际结果为：

| 范围 | 本次实际结果 |
| --- | --- |
| Rust fmt / Clippy / build | 全部通过。 |
| `cargo test --locked` | 46 个 suite 合计 841 通过、0 失败、11 项原有忽略。 |
| 合成 channel 与编译后 sender 契约 | 通过；1,100 条积压、去重、重启与 source 不变；sender 契约 1 通过。 |
| Python 3.9.6 / 3.12.14 scripts | 各 81 通过，无失败/跳过。 |
| 两个 wheel 构建与源码外安装测试 | 各 50 通过，无失败/跳过；入口和独立导入通过。 |
| Swift 参数、完整 typecheck、隔离构建及签名 | 全部通过；未运行现场激活。 |
| 运行时隔离 | 11 个活动入口/配置/helper 文件哈希和 26 条完整 outbox 均与测试前相同；无助手进程。 |

工具版本仍为 Rust/Cargo 1.97.1、Clippy 0.1.97、rustfmt 1.9.0、Swift 6.3.3、Git 2.50.1；两套 pip 分别为 26.0.1 和 26.2.1。候选 helper SHA-256 为 `6d736d62e9c9fc450c08e248762b0eacaec8e83b0b93c006a04b7bb1ab378351`，与当前活动助手区分。配套 Python/native 候选需重新受控验收；原请求的第四个 attempt 不可用于该验收。

## 2026-10-20 过程记录

本轮供现有 Agent 日常按需使用授权的私聊和测试群，验收会话查找、稳定 ID 选择、分页读取、聚焦、一次性发送、引用入站回复及状态保持。范围不包括其他账号、媒体发送、多常驻 Agent 或长期无人值守。

**当前结论：验收未完成。** 三项修复已通过完整离线矩阵并受控安装：空框精确参考图、重启后 PID 证明分离、中文发送按钮精确校验。用户在手机上确认原账号登录后，当前进程打开原授权 source，窗口 880×640、三项权限正常，独立私聊 focus 通过。但后续第四个 attempt 的初始 focus 截图再次为纯白，未产生 focus 动作标记，未进入 prepare/submit；它不是允许恢复的准备失败，原请求现保留四个 attempt，禁止继续恢复或换 ID 重放。两种截图和可访问控件均显示主窗口内容消失，“窗口 → 聊天”未恢复，原因未确定，正等待本机肉眼观察以区分界面和远程显示异常。新版 CLI 仍通过三页只读群读取；返回群、回复、成功意图幂等及独立收件确认未完成。

## 缺陷、修复与已保留证据

原助手在复制空输入框后要求剪贴板 sentinel 不变且视觉区域没有文字。现场空框显示系统输入提示，因而被判为草稿不匹配。此前尝试移开鼠标并未消除提示，相关实验代码已撤销，不能作为有效修复；“语音模式”判断同样证据不足。

本次修复增加可选私有 `empty_composer_reference`：确认没有用户草稿后，校准整个输入区域的 PNG 与 SHA-256。Python 校验私有文件、哈希和尺寸，Swift 在打开时再次校验，并逐像素比较完整区域。它不匹配提示文字、不屏蔽区域、不设置像素容差；剪贴板 sentinel、计数及原有目标、焦点和窗口检查仍须通过。最多观察三次以容纳光标闪烁。复制得到非空文字时仍必须与预期草稿精确相等。两阶段引用必须一致，恢复也不能替换已有引用。未配置引用的旧行为保持兼容。

回归用例覆盖任一像素变化、尺寸/哈希不符、异常文件权限、符号链接/硬链接、损坏图像、复制到非空文字、剪贴板变化，以及恢复时引用漂移。旧 attempt 可以在审查升级后首次增加校准，其他正文、绑定及无 submit 的证据要求保持不变。参考图不能授权恢复。

微信中文界面的发送按钮原本也被英文 `Send` 校验拒绝。现支持精确的 `Send` 或“发送”标签，并启用中文/英文 OCR；仍要求下方窗格仅有一个接受的标签，校准按钮区域内只出现该标签。重复、中英混合重复及扩展文字均拒绝。

另修复正常重启后的 PID 耦合：旧 attempt 继续使用旧 PID 核对其原 prepare/state/focus；若当前 PID 不同，要求原 inspect 完整证明旧 PID、唯一窗口、frame、权限及 console。新动作仍按当前 PID 重新 inspect/focus，不能改写历史证据或切换账号。新回归覆盖两种历史格式、原证据缺失/漂移以及只读证明不修改任何文件。安装后另用真实旧 attempt 的只读 preflight 验证旧 PID 与新 PID 分离，原证据和 outbox 均不变；该测试不证明当前身份或授权发送。

## 安装、回退与当前停止条件

- 原源码候选为 `d8f491082d9aa9faf14f40b866fdb12b88a1f6c8`，最初核对全部 11 个已安装 Python 模块逐字节相同。本文新增空框修复基于该候选；其提交与现场接受状态须分别记录，不能用旧版本证据代替。
- 原 helper SHA-256 为 `e72ea65efe2732d58fd40164f65d4a65324f558f8dbca9f4e2e68711ee5af02a`。无效鼠标实验曾安装后回退，历史三个 attempt 均保留。原签名 app、环境和配置路由仍有私有回退材料。设置页诊断使用的临时校准 helper 已由正式源码构建的助手替换，诊断代码不进入产品源码，未通过它发送消息或修改设置偏好。
- 新 helper SHA-256 为 `db8aa2be6c46acb4a88c7bb89b2a1e0ecf18f3773c533c90dc721c7d0788e473`，独立构建和严格签名校验通过。wheel 安装在单独环境；从仓库外导入核对全部 11 个模块与当前源码一致。仅更新 app、入口及明确列出的 sender 配置和路由，账号、source、channel/consumer/outbox 位置保持原值。
- 替换在 registry、controller、runner、UI 四个锁内进行，writer 已停止，无 pending/lease；全部 26 条 outbox 在替换前后完整相同。原有 25 条历史请求与本轮原请求均保留。第四次前的三份 attempt 在第四次后完整相同。代码和配置回退先做隔离演练，逐字节及权限恢复通过；早先 app 回退也已演练并实际核对。回退从不覆盖 channel/outbox/consumer 状态快照。
- 新助手在原固定路径刷新既有授权后，fresh inspect 的 Accessibility、Screen Recording、Post Events 均为 true。当时 PID 匹配、仅一个主窗口，窗口已通过微信标准“缩放窗口”恢复为原 880×640，未放宽保护。`onConsole=true`，lock flag 缺省不证明解锁。
- 两种截图接口曾同时观察到纯白主窗口。正常重启后，用户完成原账号手机登录；只读核对当前 PID 打开原授权 source、唯一窗口 880×640、三项权限为 true，并受四锁更新三份配置的 PID。独立私聊 focus 通过。此后原请求按 `expected-attempt=3` 显式恢复一次，新的第四个 attempt 再次于初始 focus 捕获纯白并停止。没有 focus 动作标记、prepare 或 submit 痕迹，原三个 attempt 完全未变。对第四次的后续只读 recovery preflight 已验证拒绝，未领取第五次。
- 原 v4 不确定请求继续保留，不属于允许恢复的准备错误，不得重试。本轮原请求的第四个 attempt 属于 focus 失败，不符合两种允许的准备错误，现同样不得恢复、普通重发或换 ID 重放。保留全部证据并继续只读诊断。

## 本轮现场范围

| 检查 | 实际结果 |
| --- | --- |
| 精确名称与稳定 ID | 两个授权目标各有一个候选，kind、ID、talker 与原绑定一致；登录恢复后须刷新。 |
| 已安装 CLI 入口 | 原候选 16 项 help/version 检查通过，恢复入口包含 `--expected-attempt`；新 wheel 的独立导入及入口检查通过。 |
| 群只读分页 | 每页 2 条读完 3 页、5 个事件，其中 1 条未处理入站；新版安装后再次通过，游标递增、不重复、最终 `has_more=false`，consumer 未推进。 |
| 私聊选择、状态、focus | 原候选通过，focus 约 3 秒；重新登录后的新安装 Python/原生入口也曾通过，随后再次出现白屏。 |
| 私聊发送 | 原意图前三个 prepare 失败，第四个 focus 失败；全部无 submit，原记录保留，第四次不可恢复。 |
| 私聊 → 群 → 读取并回复 | 待完成。 |
| 成功意图幂等及重启后的状态 | 离线回归通过；本轮现场待完成。不得重发失败意图来验证幂等。 |
| 本地正确 talker 出站及独立收件 | 本轮待完成。历史 v3 收件和本地 `ui_observed` 均不能替代。 |

## 完整离线矩阵

以 `.github/workflows/ci.yml` 为准，本次空框修复运行以下完整矩阵。macOS arm64；Rust/Cargo 1.97.1、Clippy 0.1.97、rustfmt 1.9.0、Swift 6.3.3、Git 2.50.1；Python 3.9.6 / pip 26.0.1 与 Python 3.12.14 / pip 26.2.1。

本轮实际命令采用以下路径别名；`py` 分别取 `py39` 和 `py312`，`wheel_dir` 对应矩阵中的 `wheels39` 和 `wheels312`。完整参数、版本和逐项日志保存在该忽略目录。

```sh
check_dir=.local/live-reliability-20261020/localized-reference-matrix
py39=.local/reliability-validation-20261017/python39/bin/python
py312=.local/reliability-validation-20261017/python312/bin/python
candidate_dir=.local/live-reliability-20261020/localized-reference-candidate
```

| 实际命令 | 结果 |
| --- | --- |
| `cargo fmt --check` | 通过。 |
| `cargo clippy --locked --all-targets -- -D warnings` | 通过。 |
| `cargo test --locked` | 46 个报告 suite 合计 841 通过、0 失败、11 项原有忽略。忽略项不计通过。 |
| `cargo build --locked` | 通过。 |
| `"$py312" scripts/test_local_channel.py --binary target/debug/wx-cli` | 通过：1,100 条合成积压、去重、重启确认及读取时 source 不变；不使用真实微信。 |
| `PYTHONPATH=packages/wx-agent/src "$py312" -m unittest scripts.test_macos_sender_driver.SenderTests.test_real_synthetic_channel_event_contract` | 1 通过，无失败/跳过。 |
| `PYTHONPATH=packages/wx-agent/src "$py" -m unittest discover -s scripts -p 'test_*.py'` | 两个版本各 79 通过，无失败/跳过。 |
| `"$py" -m pip wheel --no-deps --wheel-dir "$wheel_dir" ./packages/wx-agent` | 两个版本均通过。 |
| `"$py" scripts/check_package.py "$wheel_dir"/*.whl --tests packages/wx-agent/tests` | 各 50 项已安装包测试通过，无失败/跳过；在源码外运行，入口与导入通过。 |
| `WX_BRIDGE_BUILD_DIR="$PWD/$check_dir/native" native/macos/wechat-bridge/test-parameters.sh` | 参数、精确像素/私有参考图和独立命名剪贴板检查通过；单个可执行程序不报告断言数，不操作微信。 |
| `WX_BRIDGE_BUILD_DIR="$PWD/$candidate_dir" native/macos/wechat-bridge/build.sh` | 独立 app 构建与 ad hoc 严格签名校验通过。 |
| `"$py" scripts/check_public_source.py` | 两个版本均通过；另须人工审阅完整 diff。 |
| `git diff --check` | 通过。 |

完整原生源码 typecheck 也通过：

```sh
xcrun swiftc -typecheck -parse-as-library -swift-version 5 \
  -target arm64-apple-macos14.0 \
  -framework AppKit -framework ApplicationServices -framework CoreGraphics \
  -framework ScreenCaptureKit -framework Vision \
  native/macos/wechat-bridge/main.swift \
  native/macos/wechat-bridge/VisualParameters.swift \
  native/macos/wechat-bridge/ClipboardSupport.swift
```

原候选及无效实验此前也分别运行过完整矩阵，各为 Rust 841 通过/11 忽略、两个 Python 各 scripts 76 和安装包 50 通过。旧离线结果及本次离线结果都不证明桌面发送、收件或长期可靠性。

## 日常操作与停止条件

1. 用私有入口和配置通过 `find` 核对全部候选，用稳定 ID 选择并检查 `status`。只读 `read` 不启动模型或 runner；按 `next_cursor` 读到 `has_more=false` 后再决定回复。
2. 桌面操作前核对原账号、权限和校准窗口。登录、重启、helper 更新或回退后刷新这些证据。纯白画面、额外窗口或坐标变化必须停止；不得放宽 guard、清空用户草稿或手工补发。
3. 新意图先保存稳定 request ID 和正文，使用 `send --text-stdin`；回复引用真实入站 cursor，使用 `reply --text-stdin`。导入群复用原 controller/outbox/CAS，完成后恢复原启停状态。
4. failed、indeterminate、attempt_recorded 或未知结果均保留原请求、证据及 pending/lease，停止。只有两种允许的准备错误及完整证明，才可按[恢复契约](contracts.md#显式恢复已证实未提交的尝试)显式恢复原请求和精确 attempt。任何 submit 或错误目标证据都禁止恢复。
5. 结束时核对原绑定、历史 attempt、consumer、pending、lease 和 runner 状态。把本地出站与独立收件分别记录；有限现场通过不代表所有桌面状态或长期运行已验证。

真实身份、正文、参考图、截图、sender 配置、凭据和数据库材料只保存在私有忽略目录。
