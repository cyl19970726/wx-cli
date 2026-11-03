# 普通应用发送通知

`ApplicationClient` 让已获授权的本地业务应用向受管会话提交文字意图，并按原业务 key 查询结果。应用无需运行模型或安装 Session 插件。Rust coordinator 和 outbox 保存会话绑定、意图及发送尝试；多个应用共用桌面队列。架构边界见[通道订阅合同](CHANNEL_SUBSCRIPTIONS.md)。

## 本地入门

1. 安装 `wx-agent`，准备现有的 `wx-cli`、受管会话配置、coordinator root 和目标 channel state dir。确认目标绑定处于 `active`、`reply_policy=authorized`，取得该绑定的 `owner` 与 `epoch`。本客户端不会替应用更换 owner。
2. 将[合成配置样例](../examples/application-config.synthetic.json)复制到仓库外的私有文件，替换为本机**规范绝对路径**、获准的 32 位十六进制 conversation ID、当前 owner 和 epoch。配置文件必须由当前用户持有，权限为 `0600`，且不是符号链接或硬链接。`application_id` 固定为本应用的稳定命名空间。
3. 用[本地通知样例](../examples/application-notifier.py)提交一次意图，之后用同一个本地 key 查询。以下命令的路径与 ID 都是合成占位值；执行前须替换为获授权的私有配置。

```sh
printf '%s' 'Synthetic notice' | python3 examples/application-notifier.py \
  --config /ABS/private/app-a.json send \
  --conversation-id 0123456789abcdef0123456789abcdef \
  --request-id notice-001 --text-stdin

python3 examples/application-notifier.py \
  --config /ABS/private/app-a.json status \
  --conversation-id 0123456789abcdef0123456789abcdef \
  --request-id notice-001
```

另一应用使用自己的 `application_id`（例如 `app_b`）、独立配置与稳定本地 key。`app_a` 的 `notice-001` 冻结为 `app_a.notice-001`，`app_b` 的同名 key 冻结为 `app_b.notice-001`；这两个完整 ID 不冲突。`send_text()` 和 `status()` **都接收原始本地 key** `notice-001`，而不是返回的完整 ID。业务重试须沿用原 key 与完全相同的内容；改变已用 key 的正文会被拒绝。`send_text()` 使用 `--no-wait` 入队，不等待桌面执行。

`send_text()` 在新发送前检查固定的 owner、epoch、控制状态及回复授权，并将应用 ID 冻结为 Rust outbox 的 `publisher_id`。该字段与业务 owner 分开，重启或交还不会改变；已有 ID 也不能更换 publisher。`status()` 只读原会话中属于本应用 publisher 的请求，即使本应用后来续授权到新 owner/epoch，仍可查询自己的旧结果；旧记录的 `NULL` publisher 不会被冒认为本应用。查询不会重新发送。超时或返回不明时，保留原 key，检查原请求状态，不能改 key 重投。`pending` 是排队状态，`ui_observed` 是本地界面证据，均不等于收件人确认。

`ApplicationClient` 是**可信本机配置适配器**，配置限定会话范围并固定 owner/epoch。独立应用凭据和远端能力由下述 gateway 负责；本地适配器本身不隔离同一系统用户拥有任意 shell 权限的程序。图片发送后置。

## 鉴权远端接入

网关是一次性 stdin/stdout JSON RPC，不启动模型或额外消息账本。`wx-agent-gateway --config POLICY request` 校验应用 token 的 SHA-256 摘要、允许会话和权限；`wx-agent-gateway-client --config CLIENT METHOD --params-stdin` 可直接调用或经已有 SSH alias 调用它。正文和 token 都从 stdin 传送，SSH 命令不携带它们；transport 超时不自动重试。

使用[网关策略样例](../examples/gateway-policy.synthetic.json)和[客户端样例](../examples/gateway-client.synthetic.json)准备私有 `0600` 文件。为实际应用生成独立随机 token，客户端保存原值，策略只保存摘要；样例 token 仅供合成测试。策略中的 `client_config` 指向上文应用配置。`send` 允许提交，`result` 允许查询本应用原请求；`inbox` 用于明确绑定 Session 的业务订阅，须额外指定固定 `session_ref` 和 `thread_id`。只发通知的应用不授予 `inbox`。

远端客户端在样例中增加 `ssh_target`，值为已配置的 SSH alias；`command` 保持指向微信宿主上的绝对网关程序、`--config`、私有策略路径、`request` 这四项。客户端使用非交互 SSH 和连接超时，不修改 SSH 配置。

可选 `ssh_config` 指向本机规范绝对路径、权限为 `0600` 的项目私有 SSH 配置，客户端以 `-F` 读取它。给业务应用分发连接时，应提供只能执行网关请求的专用受限凭据；管理员的通用 SSH 连接不等同于应用权限边界。网关令牌仍按应用及会话限定能力，不应把管理员私钥交给业务应用。

```sh
wx-agent-gateway-client --config /ABS/private/app-client.json send_text --params-stdin \
  < /ABS/private/send-parameters.json
wx-agent-gateway-client --config /ABS/private/app-client.json result --params-stdin \
  < /ABS/private/result-parameters.json
```

发送参数只含 `conversation_id`、原本地 `request_id` 和 `text`；查询去掉 `text`。接口不接受临时 owner、执行路径或任意命令。跨群、错误凭据及无权限方法在处理前拒绝。冻结事件按 delivery 范围分页，终结旧 Session 的 transport 与重新授权新的业务处理分别核验。源码/合成检查和真实 SSH/微信收件分别记入[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)，不能互相代替。

本轮已有本地单元与合成集成检查；具体测试证据及实机边界以[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)为准。普通应用的新端到端发送不因这些检查自动视为通过。
