# 用 skill + CLI 接管一个测试群

交互 Agent 直接读取消息并撰写回复；常驻运行器负责无人交互时订阅和自动回复。两者使用同一绑定、事件日志、consumer、发件箱和需求笔记。交互命令不调用回复模型。

入口是 `wx-agent chat --config /ABS/private/agent-config.json`。配置沿用已初始化的单群控制器，指向所在机器的 Rust `wx-cli` 和发送驱动；机器、密钥、微信登录和原生助手的准备见 [开始使用](quickstart.md)。本期选择的是已经配置的那个群，不会为任意新群自动创建发送配置。

## 查找和选择

```sh
wx-agent chat --config "$CONFIG" groups --name "$GROUP_NAME" --exact
wx-agent chat --config "$CONFIG" select --talker "$EXACT_ROOM_ID"
wx-agent chat --config "$CONFIG" status
```

`groups` 只复制联系人和会话加密库及 WAL，在临时副本中读取群元数据；不读取消息分片。返回 `matches: [{name, talker}]` 和 `match_kind`。多个同名群全部返回，不擅自选第一个。未找到也是成功的空结果，不等于读取失败。

`select` 校验完整群 ID 必须等于配置对应的已有绑定；持久保存选中的频道和配置指纹。选择其他群会报错，不修改绑定或消费位置。这个选择是 CLI 的操作对象；读取不需要让微信窗口保持在该群，发送驱动才会核验并切换桌面群聊。

## 接管与分页读取

```sh
wx-agent chat --config "$CONFIG" takeover --wait-seconds 30
wx-agent chat --config "$CONFIG" read --limit 20
wx-agent chat --config "$CONFIG" read --after "$NEXT_CURSOR" --limit 20
```

接管请求后台在安全边界退出，等待进程退出并取得操作锁，再保存交互占用标记。期间的发送不会被强杀。原运行器存在未完成回复、发件箱有未尝试请求或相关不明结果时，拒绝进入人工回复，保留原记录。

占用标记存在时，普通 `start`、`resume` 和直接运行器启动会拒绝；重复接管不会覆盖已有标记。每次交互发送仍取得同一操作锁。原生发送器另外串行化桌面动作。

`read` 先同步再读取，默认从原 consumer 已处理位置之后开始。返回发送者、方向、正文、消息身份、事件 `cursor`、`next_cursor`、`has_more` 和当前已处理位置。`has_more` 为真时继续用返回的 `next_cursor` 翻页；不要用时间戳代替游标。可以传 `--after 0` 查看已有日志。**阅读和翻页不会确认消费**。本地历史仍为有界范围，`history_complete` 始终为 false。

## 回复和重试核验

```sh
wx-agent chat --config "$CONFIG" reply --cursor "$INCOMING_CURSOR" --text-stdin < "$PRIVATE_REPLY_FILE"
wx-agent chat --config "$CONFIG" status
```

回复只接受下一个待处理的入站文本；若前面还有未处理入站，返回 `earlier_incoming`，不发送、不推进进度。来源事件决定稳定请求 ID，与后台计算方式一致。重复调用同一来源、同一正文只返回已有完成结果；换正文会拒绝，不生成另一个 ID。

一次发送成功到本地 `ui_observed` 后，通过原游标的事务比较再确认消费，并同步运行器的持久位置。发送成功与确认之间崩溃时，原请求可以继续完成本地记录，但不会再次点击发送。发送结果不明会保留 pending；重复调用仅核对原结果，不能自动重发。

`ui_observed` 仍只代表本地出站证据。需要证明送达时应另行取得收件端确认。历史上与当前待处理消息无关的失败/不明记录只报告计数，不修改、不重发；与本次来源相关的不明请求会阻止交接。

## 交还后台

```sh
wx-agent chat --config "$CONFIG" handback
wx-agent chat --config "$CONFIG" status
```

先核对没有人工 pending、原 consumer 和运行器位置一致。接管前后台正在运行时，启动原配置并完成就绪握手，再解除占用；原先停止则保持停止。未被人工处理的后续消息保留给原 consumer。交还不会换账号、群、consumer 或重置游标。

启动失败会保留交还状态；修复实际原因后可以再次执行 `handback`。不得通过删除占用标记、清空发件箱或新建 consumer 来解除不明状态。底层命令见 [单群 CLI](SINGLE_GROUP_CLI.md)，操作规范见 [项目 skill](../.agents/skills/wechat-group-agent/SKILL.md)。
