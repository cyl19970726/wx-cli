# 控制面板

`wx-agent panel` 在微信宿主上启动一个只监听本机回环地址的网页，用来查看托管会话的整体状态并处理草稿。它只调用已有的 Rust coordinator、注册表和 outbox 合同，不另存状态，也不启动模型。

## 启动

```sh
/ABS/venv/bin/wx-agent panel --config /ABS/private/wx-agent/conversations/config.json --port 8765
```

命令打印一行 JSON：`{"url": "http://127.0.0.1:8765/#token=…"}`。令牌每次启动随机生成，只放在 URL 的 `#` 片段里，不会出现在请求日志中；页面读取后存入当前标签页的 sessionStorage 并从地址栏清除。所有 API 调用都要带这个令牌，并且只接受 `127.0.0.1:端口` 或 `localhost:端口` 作为 Host，防止 DNS 重绑定。`--host` 只接受 IPv4 回环地址。

从另一台电脑查看时，用 SSH 端口转发，不要把面板绑定到局域网地址：

```sh
ssh -L 8765:127.0.0.1:8765 WECHAT_HOST
```

`--approver` 是写入批准记录的操作者标签（默认 `panel`），只能是 1–64 个安全 ASCII 字符。

## 能看到什么

- **桌面状态**：共享桌面执行器是否暂停、当前执行的调度、各状态的调度数量和最近 20 条调度。
- **需要处理**：`blocked`、`unknown` 或正在执行的调度；Session 回合已结束却没有决定的消息；仍是旧单群导入记录的会话；被人工接管的会话；待审草稿。
- **会话**：注册表与 coordinator 中的全部会话，包括 owner、输入方式、回复策略、控制状态、epoch 和处理进度。
- **待办**：每个会话 `pending`、`claimed`、`deferred`、`draft` 状态的 delivery。草稿可展开查看原文与对应的源消息（最多 50 条）。

面板从不返回 inbox 领取令牌或 transport 令牌。消息正文只在本机页面中显示。

## 能做什么

| 操作 | 行为 | 不会做的事 |
| --- | --- | --- |
| 暂停桌面发送 | 调用 `coordinator dispatch pause --reason`，正在执行的一项会完成，之后不再领取新调度 | 不恢复、不审查；恢复仍需在宿主上按托管指南执行 `dispatch review` |
| 重新交给 Session | 只对"回合已结束但没有记录决定"的 delivery 可用：核对 epoch 后调用 `inbox resume`，交回 owner 的 Session 重新处理 | 不发送；不处理仍在进行或结果不明的回合 |
| 批准并发送草稿 | 1. 核对会话 epoch、控制状态，回复策略必须是 `authorized`；2. 用所选源消息游标调用 `conversations reply --no-wait`，把草稿原文冻结为托管回复意图并入队；3. 调用 `coordinator inbox approve-draft` 把该 delivery 记为 `replied` 并关联请求 ID | 不修改回复策略；不绕过共享执行器；草稿文字不可在面板中编辑 |

`inbox approve-draft` 只在以下条件全部成立时生效：delivery 仍是空闲的 `draft`，owner/epoch 未变且控制为 `active`，会话回复策略是 `authorized`，outbox 中已有同一 owner/epoch、正文与草稿逐字相同的托管文字意图，并且该请求已有发送调度。它记录 `approve_draft` 决定历史，本身不发送。重复批准同一请求返回 `already_approved: true`，不会重复入队；回复请求 ID 由源消息派生，重复操作也不会重发。

回复策略是 `draft`（只起草）的会话不能通过面板批准成发送。需要发送时，先由有权限的操作者按托管指南用 `conversations configure --reply-policy authorized --expected-epoch N` 显式授权。

批准只表示消息进入发送队列。实际结果以调度列表与 outbox 为准；`ui_observed` 是本机出站证据，不等于对方已收到。
