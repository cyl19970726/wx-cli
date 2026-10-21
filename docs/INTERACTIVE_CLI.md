# 用 skill + CLI 接管一个测试群

可以只用 CLI 按需读取历史，也可以让当前 Agent 选择性回复，或者显式启动常驻运行器持续处理该群。常驻运行器不是使用 CLI 的前提。两者使用同一绑定、事件日志、consumer、发件箱和需求笔记。交互命令不调用回复模型；读取消息不要求发送回复。

入口是 `wx-agent chat --config /ABS/private/agent-config.json`。配置沿用已初始化的单群控制器，指向所在机器的 Rust `wx-cli` 和发送驱动；机器、密钥、微信登录和原生助手的准备见 [开始使用](quickstart.md)。本期选择的是已经配置的那个群，不会为任意新群自动创建发送配置。

## 查找和选择

```sh
wx-agent chat --config "$CONFIG" groups --name "$GROUP_NAME" --exact
wx-agent chat --config "$CONFIG" select --talker "$EXACT_ROOM_ID"
wx-agent chat --config "$CONFIG" status
```

`--text-stdin` 原样读取标准输入；`< "$PRIVATE_REPLY_FILE"` 是 shell 重定向。正文内部换行保留，末尾 LF、CR 或 CRLF 在创建 pending/outbox 前被拒绝；不会静默裁剪或改写原回复。已有不确定请求仍按原 request ID 和原文保留，不可用修改文件的方式重发。

`groups` 只复制联系人和会话加密库及 WAL，在临时副本中读取群元数据；不读取消息分片。返回 `matches: [{name, talker}]` 和 `match_kind`。多个同名群全部返回，不擅自选第一个。未找到也是成功的空结果，不等于读取失败。

`select` 校验完整群 ID 必须等于配置对应的已有绑定；持久保存选中的频道和配置指纹。选择其他群会报错，不修改绑定或消费位置。这个选择是 CLI 的操作对象；读取不需要让微信窗口保持在该群，发送驱动才会核验并切换桌面群聊。

## 只读访问与分页

```sh
wx-agent chat --config "$CONFIG" read --limit 20
wx-agent chat --config "$CONFIG" read --after "$NEXT_CURSOR" --limit 20
```

单纯查看历史不需要 `takeover` 或 `handback`，不会自动启停后台。`read` 先同步再读取，默认从原 consumer 已处理位置之后开始。返回发送者、方向、正文、消息身份、事件 `cursor`、`next_cursor`、`has_more` 和当前已处理位置。`has_more` 为真时继续用返回的 `next_cursor` 翻页；不要用时间戳代替游标。可以传 `--after 0` 查看已有日志。**阅读和翻页不会确认消费**。本地历史仍为有界范围，`history_complete` 始终为 false。

## 取得处理权

```sh
wx-agent chat --config "$CONFIG" takeover --wait-seconds 30
```

接管请求后台在安全边界退出，等待进程退出并取得操作锁，再保存交互占用标记。期间的发送不会被强杀。原运行器存在未完成回复、发件箱有未尝试请求或相关不明结果时，拒绝进入人工回复，保留原记录。

占用标记存在时，普通 `start`、`resume` 和直接运行器启动会拒绝；重复接管不会覆盖已有标记。每次交互发送仍取得同一操作锁。原生发送器另外串行化桌面动作。

当前高层交互入口沿用已有初始化控制器；为全新群建立独立配置仍遵循初始化流程。它尚不是任意群的一键注册器。已有控制器停止时也可以取得处理权，之后交还保持停止。

## 回复和重试核验

```sh
wx-agent chat --config "$CONFIG" reply --cursor "$INCOMING_CURSOR" --text-stdin < "$PRIVATE_REPLY_FILE"
wx-agent chat --config "$CONFIG" status
```

回复只接受下一个待处理的入站文本；若前面还有未处理入站，返回 `earlier_incoming`，不发送、不推进进度。来源事件决定稳定请求 ID，与后台计算方式一致。重复调用同一来源、同一正文只返回已有完成结果；换正文会拒绝，不生成另一个 ID。

一次发送成功到本地 `ui_observed` 后，通过原游标的事务比较再确认消费，并同步运行器的持久位置。发送成功与确认之间崩溃时，原请求可以继续完成本地记录，但不会再次点击发送。发送结果不明会保留 pending；重复调用仅核对原结果，不能自动重发。

`ui_observed` 仍只代表本地出站证据。需要证明送达时应另行取得收件端确认。历史上与当前待处理消息无关的失败/不明记录只报告计数，不修改、不重发；与本次来源相关的不明请求会阻止交接。

### 操作者恢复已证实未提交的回复

一般的 `failed`、`indeterminate` 或未知结果仍然停止处理。只有已核实原次发送未提交，且原生私有证据完整匹配、没有 submit 记录且没有存活 helper 时，才允许显式调用窄范围恢复命令。当前只接受两种准备失败：`Cannot snapshot a clipboard item` 和 `Copied composer text does not exactly match expected draft`。其他错误、身份/正文/游标不一致、证据缺失或任何 submit 痕迹都必须停止；错误目标发送（P3）永远不能重放。

对仍保留在原 lease 的回复，先从该请求的 outbox 状态取得当前 attempt number，再由操作者明确指定原 cursor 和相同的 attempt：

```sh
wx-agent chat --config /ABS/private/wx-agent/group-controller.json \
  recover-unsent-reply --cursor 121 --expected-attempt 1
```

命令复用 lease 中原 request ID 与正文。底层先完成 driver capability 握手和只读 preflight，再以 CAS 领取下一不可变 attempt；driver 随后在共用 UI 锁内重新证明原尝试未提交、保存 proof 并最多进行一次发送尝试。任一步缺少证据都失败关闭，不自动退回普通发送。恢复后再次失败或结果不明时保留新旧记录并停止；禁止自动重试、换 ID 或连续尝试。恢复完成不代表收件确认。

该命令仅在包含恢复协议的 Python 包版本中可用。部署到实际 Mac 后先核对该机 `wx-agent chat recover-unsent-reply --help` 显示必需的 `--expected-attempt`；当前源代码/离线测试通过不等于远程运行时已升级。协议细节见[发件箱恢复契约](contracts.md#显式恢复已证实未提交的尝试)。

## 已处理但无需回复

```sh
wx-agent chat --config "$CONFIG" skip --cursor "$INCOMING_CURSOR" --reason "普通确认，无需回复"
```

`skip` 只处理下一个未处理入站文本，以原 consumer 的 CAS 确认消费并保留来源游标和原因。它不创建发件箱请求，不调用模型或 GUI；原文仍在来源日志中。相同游标和原因的重复调用返回已有结果，失败恢复沿用原 skip 意图。已有回复请求、未决发送、不同处理原因或跳过前面的未处理入站都会被拒绝。

当前 Agent 应先读完整段上下文，再决定是否说话。连续补充可以把前面的背景消息标为无需回复，只对需要回应的那条回复一次。常驻适配器已有 `{ "skip": true }` 输出能力；自动判断质量仍取决于具体 Agent 和群策略，本轮不把一个显式 skip 测试当作模型在所有聊天场景都判断正确的证明。

## 交还后台

```sh
wx-agent chat --config "$CONFIG" handback
wx-agent chat --config "$CONFIG" status
```

先核对没有人工 pending、原 consumer 和运行器位置一致。接管前后台正在运行时，启动原配置并完成就绪握手，再解除占用；原先停止则保持停止。未被人工处理的后续消息保留给原 consumer。交还不会换账号、群、consumer 或重置游标。

启动失败会保留交还状态；修复实际原因后可以再次执行 `handback`。不得通过删除占用标记、清空发件箱或新建 consumer 来解除不明状态。底层命令见 [单群 CLI](SINGLE_GROUP_CLI.md)，操作规范见 [项目 skill](../.agents/skills/wechat-group-agent/SKILL.md)。
