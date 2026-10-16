# 通用会话选择与自动切换

用户授权：在现有 CLI 上支持动态选择群聊与个人会话；经确认后向指定测试联系人发送测试消息，再切回测试群验证读取和回复。授权不包含访问其他聊天，也不等于新路径现场验收已完成。

## 实现状态

| 交付项 | 状态 | 依据与限制 |
| --- | --- | --- |
| Rust 元数据发现群与个人联系人，稳定 ID，保留重名候选 | 已实现 | `wx-cli conversations find`；ID 由账号和 talker 派生。 |
| Python CLI 注册/选择隔离 channel、状态、读取和手动操作 | 已实现 | `wx-agent conversations`；读取不 ack，新会话 reply 只发送不推进 consumer。 |
| 复用已有单群 controller，桌面写操作协调原 writer | 已实现 | 只导入显式配置且绑定匹配的 controller；不提供多常驻 Agent 调度。 |
| GUI 目标验证与重名拒绝 | v5 部署、权限、有限导航和只读读取已通过；v5 发送未验证 | v4 私人返回群、私人/群 focus 曾通过；随后群 send 的 focus 失败于 prepare/submit 前，原 request 不重试。失败时无 popup PNG，失败后窗口为 368×500，具体故障阶段和原因未确定。v5 search budget 从 2.5 秒提高到 6 秒，并加入非消息窗口元数据诊断；目标保护条件未放宽。v5 权限和有限 navigation/read 已通过；v5 send 与长期稳定性未验证。桌面 UI 无原生会话 ID；操作期间须无人手动切换，且相同渲染标题仍有 TOCTOU 风险，不得称绝对安全或生产级。 |
| 显式恢复已证实未提交 | 已实现并受限现场验证 | 恢复协议仅接受两个剪贴板/草稿准备错误，通过能力握手、只读证据预检、UI 锁内重新证明和 CAS。两条获准 intent 各仅恢复一次且收件端确认；P3 含 submit 证据的错误目标意图被拒绝。 |
| 文档与 skill | 已更新 | 见[会话 CLI](CONVERSATIONS_CLI.md)、[操作指南](OPERATING_GUIDE.md)及项目 skill。 |

## 验证状态

- `31349d9` 修复标题 OCR 区域误命中，独立审查通过；v3/v4 权限曾现场复核全为启用。后续 focus 失败时没有 popup PNG，失败后观测到的窗口为 368×500，故障时点/原因未知。v5 native helper 已部署（source commit、SHA 见验证记录），Python 包未变；延长 search budget 并采集非消息窗口元数据，不放宽目标守卫。v5 权限、有限导航和只读读取已验证；v5 发送与长期稳定性未验证。
- 对误命中场景的回归检查通过：当目标显示名仅出现在另一会话的消息发送者区域时拒绝目标；真实标题区域匹配时接受。独立审查未发现遗留问题。
- 现场确认授权测试个人会话有两条入站消息，CLI 按正确发送者和顺序读取。用户确认两条各自授权并恢复的意图分别只收到一次；重复原 `chat reply` / `conversations reply` cursor 命令返回 `already_completed`，没有重跑 recovery。
- 用户确认先前 PRIVATE-OK 与群 CLI 回复各收到一次；另确认 v3 合成群/私人消息各收到恰好一次，两条都各自读回一条。重复群 send 请求返回 `already_completed`，没有再次操作 GUI。v4 返回群和双向 focus 曾现场通过，但随后群 send focus 再次失败，无法证明搜索稳定性。
- 一次修复前的测试发送实际落入了测试群，而非目标个人会话。该 P3 意图含 submit 证据，preflight 拒绝恢复；不重放、不作为私聊发送验收。
- “个人会话正确发送 → 回到测试群 → 读取并回复”的完整往返仍未通过：v3 私人发送和读回成功，v4 focus 有成功记录但随后群发送 focus 失败，未进入 prepare/submit，原 request 保留且不得重试。v5 权限、有限导航和读取已通过，但 v5 send 未测试、长期稳定性未证明。多常驻 Agent、逐会话模式表、共享桌面多群调度和 MCP 不在本轮交付范围。详见[现场验证记录](CONVERSATIONS_VALIDATION_20261016.md)。

具体时间线、已知错误和剩余验收见[现场验证记录](CONVERSATIONS_VALIDATION_20261016.md)。记录中不包含真实名称、账号或群 ID、正文、密钥、配置或截图。

## 安全与升级边界

最近的目标误判来自 helper 将 `y=0..147` 的区域当作完整标题；在 `880×640` 校准窗口中，这一区域也覆盖了消息发送者信息。因而即使会话元数据和唯一名称检查通过，helper 仍可能把消息发送者误认为当前聊天标题。`31349d9` 要求 OCR 命中完整校准标题框，且回归拒绝错误区域的匹配。

本轮是针对具体误目标缺陷和导航问题的受控原生 helper 升级例外：旧签名应用先备份，helper 路径固定，更新两份私有配置 digest 并保留 channel/outbox/consumer/lease 状态。当前部署、权限和 live 验证证据见[验证记录](CONVERSATIONS_VALIDATION_20261016.md)。不得降级或绕过窗口保护继续发送。

继续遵循单写者、唯一目标、outbox 一次尝试及未知结果不重发。多常驻 Agent、按会话模式注册表、共享桌面多群调度和 MCP 不在本轮交付范围内。
