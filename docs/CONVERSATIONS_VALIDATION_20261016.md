# 通用会话 CLI 验证记录 · 2026-10-16

最新实际安装核验与现场结果见 [2026-10-20 验收记录](CONVERSATIONS_ACCEPTANCE_20261020.md)。以下保留此前各版本的历史证据，不代表当前安装状态。

2026-10-17 的源码可靠性修复、完整离线矩阵及待审部署方案见[候选验证记录](CONVERSATIONS_RELIABILITY_20261017.md)。候选尚未替换现场 v5，不改变下述历史发送验收范围。

**当前状态：v5 native helper、权限、有限导航与分页读取已现场验证。v5 未发送新消息；完整往返与长期稳定性未验证。** v3 私人/群消息各一条由用户确认收到一次。v4 返回群和私/群 focus 曾通过，但之后群发送在 focus 阶段失败、未进入 prepare/submit；原 request 保留且不得重试。此前将此故障归因于 368×194 弹窗低于 250 px 门槛的说法没有足够证据：失败时没有 popup PNG，失败后观察到的弹窗是 368×500，准确失败时点未知。v5 将 bounded search 延长至 6 秒并记录非消息窗口元数据，不放宽目标校验。最新 outbox 有 3 条历史 uncertain、无 blocked item，runner stopped；v5 未发送消息，完整往返与长期稳定性未通过验证。本记录不包含真实账号/会话标识、正文、密钥、sender 配置或截图；私有证据保留在运行主机。

## 当前证据

| 检查 | 状态 | 证据与边界 |
| --- | --- | --- |
| 元数据与只读读取 | 通过 | 授权个人会话的两条入站按正确发送者和顺序读取；v3 群发送后 `read --after 40` 返回 3 events，`next_cursor=43`、`has_more=false`，目标合成 marker 的 outgoing 事件恰好一条。v3 私人 send 后只读回 marker 恰好一条，`next_cursor=4`。读取不确认 consumer。 |
| v3 导航部署与权限 | 历史现场通过 | v3 native helper 与 wheel 曾在原固定路径运行，状态保留、两份私有配置更新。fresh inspect 确认 Accessibility、Screen Recording、Post Events 均启用。权限恢复通过 Screen Sharing/System Settings 移除旧 Accessibility 项并从固定路径添加当前应用；未选旧备份。 |
| v4 导航部署与权限 | 历史现场通过 | v4 native helper 曾部署；fresh inspect 曾确认 Accessibility、Post Events、Screen Recording 全 true。v4 发送期间的搜索故障尚未确定发生时点。 |
| v5 搜索诊断升级 | 已部署并现场验证 | source commit `fa3187b6f775f6ffcc0d8c10f9f0c3f83d92e72b`；native helper SHA-256 `a93c331d4fb99b0236fab004daa38e2050eab1d81c5a913316be3e0f2fee82f6`。Python 包未变化；状态保留。fresh inspect 确认 Accessibility、Screen Recording、Post Events 均启用，远端 `onConsole=true`；lock flag 缺省，不能据此断言屏幕已解锁。 |
| v3 私人/群 focus | 历史通过 | 分类搜索来源为 search，群类别为 Most used；私人 focus 2.812 秒、群 focus 3.185 秒。GUI 核验群标题与空草稿。 |
| v4 私人与群导航 | 间歇性通过，稳定性未通过 | 私聊返回群 focus 3.505 秒，随后私人 focus 2.810 秒、群 focus 3.230 秒；三次均 `focused=true`，类别/种类精确匹配。群类别为 Most used。群 `read --after 40` 返回 exit 0、`next_cursor=43`、`has_more=false`。紧接着的群发送又在 focus 阶段失败，不能将这三次成功作为稳定性证据。 |
| v5 live navigation 与只读读取 | 通过（有限探测） | v5 私人 focus 2.789 秒、群 focus 3.553 秒；`read --after 40` 返回 3 events，`next_cursor=43`、`has_more=false`；读取后群 focus 4.331 秒。来源为分类搜索，Most used 群分类，目标保护未放宽。弹窗首次出现分别为 334 ms、582 ms、1408 ms。证据在受控主机的 v5 JSON 记录中。此次未发送消息；不证明重复运行或长时间稳定。 |
| v3 新合成群发送 | 通过，收件端确认 | CLI 返回 `ui_observed`，耗时 10.023 秒；用户确认恰好收到一条。相同 request ID 和正文重复调用返回 `already_completed=true`（0.678 秒），没有再次操作 GUI。 |
| v3 新合成私人发送 | 通过，收件端确认 | CLI 返回 `ui_observed`，耗时 8.874 秒；用户确认恰好收到一条。之后只读回 marker 恰好一条。 |
| 私人发送后返回群（旧版本） | 曾失败，原因未确定 | 返回 focus 报 `Exact target group absent from bounded search results`。失败时没有 popup PNG；失败后窗口观察为 368×500。此前“368×194 小窗低于 250 px 门槛”解释缺少故障时刻的观测，不能作为根因。后续 v4 有一次返回群 focus 成功，但随后再度失败。 |
| v4 新合成群发送 | 失败，未提交 | 原 request 在 5.362 秒后于 focus 阶段返回 `Exact target group absent from bounded search results`；发生在 prepare/submit 之前，未进入消息草稿准备或提交步骤，没有发送消息。保留原 request，不重试、不更换 ID；该目标搜索错误不符合 `recover-unsent` 允许的准备错误。不得将旧 250 px 门槛解释为已确认根因。 |
| 早先两条获准回复 | 用户各确认收到一次 | 早先 PRIVATE-OK 与群 CLI 回复各收到一次；不是 v3 私人发送验收。不得重发。 |
| 修复前 P3 误目标尝试 | 安全拒绝恢复 | 一次测试消息实际落在测试群而非目标私聊。只读 preflight 因原尝试已有 submit 证据而拒绝恢复；不得恢复、重放或作为私聊发送验收。 |
| 本地运行状态快照 | 观察值 | 最近状态报告为 `outbox_blocked_count=0`、`historical_uncertain_count=3`；runner stopped。历史 uncertain 仍保留，不能表述为完全清空。 |
| 离线回归 | 通过 | 安装 wheel 测试 49 项、scripts 测试 74 项；native 参数/typecheck/build 检查已通过。离线检查不替代完整现场往返。 |

## 已确认的目标缺陷与导航限制

旧 helper 的 OCR 区域 `y=0..147` 在 `880×640` 校准窗口中覆盖标题和消息发送者信息。当目标私聊名恰好出现在另一会话的发送者位置，helper 可能把发送者错当标题。修复提交 `31349d9` 改为检查完整校准标题框；回归拒绝发送者区域匹配并接受真实标题，且独立审查通过。

随后几何修正把弹窗阴影溢出容差从 5 px 调为 16 px：实测底部溢出 9 px，边界回归接受 9 px、拒绝 17 px（这是像素数，不是测试数量）。v3 支持微信的 Most used 分类。导航按 Contacts、Group Chats 与 Most used 搜索，focus 绑定 120 秒内的新鲜搜索证据，每个视觉阶段核对标题与会话类型，群另核对成员数，并移除 current/visible 快捷路径。一次 focus 失败未生成 popup PNG；失败后的 GUI 检查看到 368×500 窗口，无法据此判断失败发生在 OCR 弹窗检查还是更早步骤，精确时点未知。此前以 368×194 小窗推断 250 px 高度门槛是根因的说法已撤回。v5 native helper 将 bounded search 时间预算从 2.5 秒增至 6 秒，并添加非消息内容的窗口元数据诊断；目标身份/标题/类型保护条件不变。现场验证已通过 v5 私人/群定向、分页读取及读后重新定向；未进行 v5 发送。Python 包未变化，不得重试 v4 未提交 request。

这是桌面 UI 自动化，无法读取微信原生当前会话 ID。一次操作期间须使用专用桌面且无人手动切换；若不同类型会话具有完全相同的渲染标题，仍有 TOCTOU 风险。不得称绝对防错、生产级隔离或长期无人值守可靠。

## 恢复规则与剩余范围

未知发送结果不自动重试、不换 request ID。`recover-unsent` 只允许对原 request、正文、绑定及精确 attempt，由操作者针对两种已记录的准备错误显式发起；必须通过能力握手、只读 preflight、完整证据匹配、无 submit/无 live helper，并在 UI 锁内再次证明。任一证据缺失或不匹配即停止。P3 错误目标发送绝不恢复。重复原 reply 命令的 `already_completed` 与重复 recovery 是不同操作；本轮 v3 重复的是原群 send 请求。

恢复剪贴板失败时的观察：关闭 Screen Sharing 自动共享剪贴板后，手动 Get/Send 控件可用；从远端微信复制普通合成文本，再执行显式恢复后成功。这个顺序与成功相关，但不能证明单一步骤是唯一原因。测试期间保持 shared clipboard 关闭，完成后恢复原设置；不得复制其他用户消息来绕过保护。

本轮完成了 v3 私人及群 focus、各一条合成消息发送/读回和用户收件确认；v4 权限复核、私聊返回群及双向 focus 曾通过三次现场检查，但后续群发送的 focus 再次失败，原请求未进入 prepare/submit，不能重试。此前 368×194/250 px 根因推断已撤回；v5 bounded search diagnostics 的权限、导航与只读读取已验证。v5 发送未测试，连续私聊到群回复流程与长期稳定性未闭环。不得绕过搜索保护或手动 GUI 补足验收。多常驻 Agent、逐会话模式表、共享桌面多群调度、MCP 和长期无人值守仍不在已交付范围。
